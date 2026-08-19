"""
Tests for the OpenAI-compatible HTTP endpoint, streaming and classic
mode, via a real ASGI request (httpx.ASGITransport) against the FastAPI
application.
"""

import json

import httpx
import pytest
import respx

from tests.fixtures.llm_sse import (
    reasoning_response,
    reasoning_tool_call_response,
    text_response,
    tool_call_response,
)


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


@pytest.fixture
def mock_side_services():
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


@pytest.mark.asyncio
async def test_non_streaming_endpoint_returns_full_answer(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["Bon", "jour"]))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Salut"}], "stream": False},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Bonjour"
    assert body["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_streaming_endpoint_returns_slash_command_result_not_empty_answer_notice(mock_side_services):
    """
    Non-regression (real bug observed via Open WebUI, which always
    streams): a slash command (app/graph.py, run_slash_command_direct)
    never invokes the LLM, so no on_chat_model_stream event is ever
    emitted during _stream_response's loop — without the fix, the
    PERSISTED final message (which does contain the real answer) was
    ignored, wrongly replaced by the "réponse non exploitable" notice
    (the code only checked what had been streamed, never the persisted
    state). Non-streaming mode
    (test_non_streaming_endpoint_returns_full_answer, _current_answer)
    didn't suffer from this bug, which affects ONLY streaming — hence its
    late discovery, since Open WebUI always streams.
    """
    import app.graph as g
    import app.main as main_mod

    mock_side_services.get("http://fake-mcp-client/tools/schema").mock(
        return_value=httpx.Response(
            200,
            json={"tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]},
        )
    )
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "Firefox\nFoot"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "/read_file"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)

    payloads = [json.loads(line[len("data: "):]) for line in lines if line.startswith("data: {")]
    contents = [p["choices"][0]["delta"].get("content", "") for p in payloads]
    full_text = "".join(contents)
    assert full_text == "Firefox\nFoot"
    assert "réponse non exploitable" not in full_text


@pytest.mark.asyncio
async def test_streaming_endpoint_splits_large_content_into_multiple_sse_lines(mock_side_services):
    """
    Non-regression (real error observed via Open WebUI: "Got more than
    131072 bytes when reading" — aiohttp's client-side line-size limit):
    large content (e.g. a base64 data-URI image for a slash command on
    browser_take_screenshot, see app/graph.py, run_slash_command_direct) sent as a
    SINGLE SSE line exceeds this limit. app/main.py's
    _sse_content_chunks must split it into several small SSE lines, like
    real token-by-token streaming would.
    """
    import app.graph as g
    import app.main as main_mod

    big_content = "x" * 300_000  # > 131072 bytes, > several times _SSE_CONTENT_CHUNK_SIZE

    mock_side_services.get("http://fake-mcp-client/tools/schema").mock(
        return_value=httpx.Response(
            200,
            json={"tools": [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]},
        )
    )
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": big_content}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "/read_file"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)

    data_lines = [line for line in lines if line.startswith("data: {")]
    assert all(len(line.encode("utf-8")) < 131072 for line in data_lines)
    assert len(data_lines) > 1  # properly split into several pieces, not one giant block

    payloads = [json.loads(line[len("data: "):]) for line in data_lines]
    full_text = "".join(p["choices"][0]["delta"].get("content", "") for p in payloads)
    assert full_text == big_content


@pytest.mark.asyncio
async def test_non_streaming_endpoint_renders_slash_command_image_without_persisting_it(mock_side_services):
    """
    End-to-end non-regression (real bug via Open WebUI): /browser_take_screenshot
    must display the image in THIS turn's HTTP response
    (_render_visible_answer, app/main.py), without the PERSISTED
    assistant message containing the base64 — otherwise a second normal
    turn on this same thread blows up the LLM's context by
    re-tokenizing it as raw text (see app/graph.py,
    run_slash_command_direct, and tests/test_slash_commands.py::
    test_slash_command_image_only_result_persists_light_text_only for the
    graph-side part of this same non-regression).
    """
    import base64
    import io

    from PIL import Image

    import app.graph as g
    import app.main as main_mod

    png_buf = io.BytesIO()
    Image.new("RGB", (2, 2), color="green").save(png_buf, format="PNG")
    png_b64 = base64.b64encode(png_buf.getvalue()).decode()

    mock_side_services.get("http://fake-mcp-client/tools/schema").mock(
        return_value=httpx.Response(
            200,
            json={"tools": [{"type": "function", "function": {"name": "browser_take_screenshot", "parameters": {}}}]},
        )
    )
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(
            200, json={"content": [{"type": "image", "data": png_b64, "mimeType": "image/png"}]}
        )
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "/browser_take_screenshot"}], "stream": False},
        )

    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert f"data:image/png;base64,{png_b64}" in content  # visible in THIS response

    # But the message persisted by the graph stays light, without the base64.
    config = {"configurable": {"thread_id": main_mod._derive_thread_id([main_mod.ChatMessage(role="user", content="/browser_take_screenshot")])}}
    snapshot = await g.agent_graph.aget_state(config)
    assert png_b64 not in snapshot.values["messages"][-1].content


@pytest.mark.asyncio
async def test_streaming_endpoint_yields_sse_chunks_and_done(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["Bon", "jour", " !"]))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Salut"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)

    assert lines[-1] == "data: [DONE]"
    assert any('"content": "Bon"' in l for l in lines)
    assert any('"finish_reason": "stop"' in l for l in lines)


@pytest.mark.asyncio
async def test_streaming_endpoint_closes_dangling_think_tag_before_approval_text(mock_side_services):
    """
    Non-regression: when the model reasons before deciding to call a
    tool, the turn ends with empty real content (the tool_call arrives
    on a separate channel) — no "real" content chunk ever arrives to
    trigger closing <think> (see _convert_delta_with_reasoning in
    app/graph.py). Without the fix, the "⚠️ Approbation requise" text
    added next ended up concatenated INSIDE the never-closed <think> on
    the client side — invisible outside Open WebUI's collapsed thinking
    bubble, and therefore unfindable by any automation (approval button)
    looking for this text in the message content.
    """
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(
            reasoning_tool_call_response(["Je vais ", "utiliser l'outil."], "browser_navigate", "call_1", '{"url": "http://example.com"}')
        )
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    chunks = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Capture le bureau"}], "stream": True},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and "[DONE]" not in line:
                    payload = json.loads(line[6:])
                    content = payload["choices"][0]["delta"].get("content")
                    if content:
                        chunks.append(content)

    full_text = "".join(chunks)
    assert "<think>" in full_text
    assert "</think>" in full_text
    # the tag must be closed BEFORE the approval text, not just present
    # somewhere in the stream
    assert full_text.index("</think>") < full_text.index("Approbation requise")


@pytest.mark.asyncio
async def test_streaming_endpoint_merges_think_across_auto_approved_tool_loop(mock_side_services):
    """
    Non-regression: with AUTO_APPROVED_TOOLS, call_llm can run several
    times in a row with no approval pause (GhostDesk capture/click loop).
    Each iteration reasons (the "reasoning" field) before deciding what
    to do next. Without carrying the <think> state over from one
    call_llm invocation to the next (AgentState.think_opened/
    think_closed), each iteration would reopen its own <think> tag in
    the middle of the stream — Open WebUI only renders the very first
    one as a collapsible bubble, later ones appearing as raw visible text
    in the middle of the answer.
    """
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(
            reasoning_tool_call_response(
                ["Je vais ", "écrire."], "write_file", "call_1", '{"path": "/workspace/x.txt", "content": "y"}'
            )
        ),
        _sse_response(reasoning_response(["Et ", "voilà."], ["Écrit", "."])),
    ]
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    chunks = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Écris ce fichier"}], "stream": True},
        ) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: ") and "[DONE]" not in line:
                    payload = json.loads(line[6:])
                    content = payload["choices"][0]["delta"].get("content")
                    if content:
                        chunks.append(content)

    full_text = "".join(chunks)
    # A single opening/closing tag despite two call_llm iterations: both
    # turns' reasoning must fit in the same block.
    assert full_text.count("<think>") == 1
    assert full_text.count("</think>") == 1
    assert full_text.index("<think>") < full_text.index("</think>") < full_text.index("Écrit.")


@pytest.mark.asyncio
async def test_streaming_endpoint_recovers_from_llm_connection_error(mock_side_services):
    """
    Non-regression: if the streamed call to the LLM fails along the way
    (e.g. llama-server cutting the connection), _stream_response must not
    die mid-way through the "Transfer-Encoding: chunked" stream without
    ever sending the terminal chunk — on the client side (aiohttp, via
    Open WebUI), this shows up as "TransferEncodingError: Not enough data
    to satisfy transfer length header", a symptom of an unhandled server
    crash rather than a real client-side network error. The SSE stream
    must end cleanly (visible error notice + finish_reason + [DONE]) even
    in this case.
    """
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("boom")
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    lines = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Salut"}], "stream": True},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)

    assert lines[-1] == "data: [DONE]"
    assert any('"finish_reason": "stop"' in l for l in lines)
    assert any("Erreur interne" in l for l in lines)


@pytest.mark.asyncio
async def test_non_streaming_endpoint_pauses_for_approval(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}'))
    )
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ?"}], "stream": False},
        )

    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert "Approbation requise" in content
    assert mcp_route.call_count == 0


@pytest.mark.asyncio
async def test_non_streaming_endpoint_reports_iteration_limit_notice(mock_side_services, monkeypatch):
    """
    Non-regression: before this fix, a run that hit MAX_TOOL_ITERATIONS
    with a tool_call still pending (auto-approved read loop) just
    rendered the model's last reasoning text as-is, with no indication
    the task had been interrupted — observed in real usage (the agent
    seemed to "stop" mid-sentence).
    """
    import app.graph as g
    import app.main as main_mod

    monkeypatch.setattr(g, "MAX_TOOL_ITERATIONS", 2)
    monkeypatch.setattr(main_mod, "MAX_TOOL_ITERATIONS", 2)

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("read_file", f"call_{i}", '{"path": "/workspace/x.txt"}')) for i in range(3)
    ]
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Relis en boucle"}], "stream": False},
        )

    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert "Limite d'itérations" in content
    assert "read_file" in content


@pytest.mark.asyncio
async def test_non_streaming_endpoint_reports_empty_answer_notice(mock_side_services):
    """
    Non-regression (real bug observed in real usage, see the README's bug
    table): a model can end a turn with no structured tool_calls AND no
    visible answer text (its whole output fit in the reasoning, e.g. a
    tool-call attempt written in prose never recognized as a real
    tool_calls). Without this fix, the agent just answers
    "<think>...</think>" — empty once the reasoning bubble is collapsed,
    with no indication anything went wrong.
    """
    import app.main as main_mod
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(reasoning_response(["Je vais ", "réfléchir."], []))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Fais quelque chose"}], "stream": False},
        )

    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert "réponse exploitable" in content


@pytest.mark.asyncio
async def test_streaming_endpoint_reports_empty_answer_notice(mock_side_services):
    """Counterpart of the non-streaming test above, on the SSE stream side."""
    import app.main as main_mod
    import app.graph as g

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(reasoning_response(["Je vais ", "réfléchir."], []))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        content = await _stream_contents(client, [{"role": "user", "content": "Fais quelque chose"}])

    assert "réponse exploitable" in content
    # the <think> tag must be closed BEFORE the notice, like for the
    # other notices (approval, iteration limit)
    assert content.index("</think>") < content.index("réponse exploitable")


@pytest.mark.asyncio
async def test_non_streaming_endpoint_resumes_after_approval_reply(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ? Site : http://example.com"}], "stream": False},
        )
        assert "Approbation requise" in first.json()["choices"][0]["message"]["content"]

        # Open WebUI resends the full history, including the approval
        # question and the user's answer, on the next turn.
        second = await client.post(
            "/v1/chat/completions",
            json={
                "model": "agent-llm",
                "messages": [
                    {"role": "user", "content": "Question ? Site : http://example.com"},
                    {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
                    {"role": "user", "content": "approuver"},
                ],
                "stream": False,
            },
        )

    assert mcp_route.call_count == 1
    assert second.json()["choices"][0]["message"]["content"] == "Resultat: 42."


@pytest.mark.asyncio
async def test_approve_endpoint_resumes_without_text_reply(mock_side_services):
    """
    /approve lets an approval pause be resumed from a button click (Open
    WebUI Action function) rather than the "approuver" text message
    expected by /v1/chat/completions. The next normal turn must not
    duplicate the history (same owui_message_count bookkeeping as the
    text flow, see _resolve_run).
    """
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
        _sse_response(text_response(["Autre", " reponse."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ? Site : http://example.com"}], "stream": False},
        )
        approval_text = first.json()["choices"][0]["message"]["content"]
        assert "Approbation requise" in approval_text

        approved = await client.post(
            "/approve",
            json={
                "messages": [
                    {"role": "user", "content": "Question ? Site : http://example.com"},
                    {"role": "assistant", "content": approval_text},
                ],
                "approved": True,
            },
        )
        assert approved.status_code == 200
        assert approved.json()["content"] == "Resultat: 42."
        assert mcp_route.call_count == 1

        # Next normal turn: Open WebUI resends its history as-is (no
        # "approuver", since the decision went through the button). Must
        # neither duplicate nor lose messages.
        second = await client.post(
            "/v1/chat/completions",
            json={
                "model": "agent-llm",
                "messages": [
                    {"role": "user", "content": "Question ? Site : http://example.com"},
                    {"role": "assistant", "content": "Resultat: 42."},
                    {"role": "user", "content": "Autre question ?"},
                ],
                "stream": False,
            },
        )

    assert second.json()["choices"][0]["message"]["content"] == "Autre reponse."

    thread_id = main_mod._derive_thread_id([type("M", (), {"role": "user", "content": "Question ? Site : http://example.com"})()])
    snapshot = await g.agent_graph.aget_state({"configurable": {"thread_id": thread_id}})
    # human1, AI(tool_call), tool, AI(final), human2, AI(other): exactly 6, no duplicate
    assert len(snapshot.values["messages"]) == 6


@pytest.mark.asyncio
async def test_approve_endpoint_grant_session_field_auto_approves_next_call(mock_side_services):
    """
    /approve accepts an optional grant_session field (Phase 3), mirroring
    the "approuver pour la session" text reply on the /v1/chat/completions
    side: the second call of the same tool (here key_type, TIER_SENSITIVE
    by default) must no longer trigger a pause.
    """
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("key_type", "call_1", '{"text": "Ceci est un texte assez long pour rester sensible par defaut"}')),
        _sse_response(tool_call_response("key_type", "call_2", '{"text": "Un second texte tout aussi long pour verifier le comportement"}')),
        _sse_response(text_response(["Fini", "."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Tape hello"}], "stream": False},
        )
        approval_text = first.json()["choices"][0]["message"]["content"]
        assert "session" in approval_text.lower()

        approved = await client.post(
            "/approve",
            json={
                "messages": [
                    {"role": "user", "content": "Tape hello"},
                    {"role": "assistant", "content": approval_text},
                ],
                "approved": True,
                "grant_session": True,
            },
        )
        assert approved.status_code == 200
        assert approved.json()["content"] == "Fini."

    assert mcp_route.call_count == 2  # key_type call_1 AND call_2, no new pause between the two


@pytest.mark.asyncio
async def test_approve_endpoint_returns_409_without_pending_approval(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(text_response(["OK"]))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Salut"}], "stream": False},
        )
        resp = await client.post(
            "/approve",
            json={"messages": [{"role": "user", "content": "Salut"}], "approved": True},
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_pending_endpoint_reports_status_without_side_effects(mock_side_services):
    """
    /pending only depends on the first human message (thread_id) — never
    on the last assistant message's content, which can be empty or
    truncated on the client side depending on how it interpreted the
    <think> tags (observed under real conditions with Open WebUI: the
    text shown on screen and the "content" of the message as returned to
    a third-party integration can diverge). This is what lets a UI
    button know whether an approval is pending without relying on this
    potentially empty content.
    """
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}'))
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_thread_yet = await client.post(
            "/pending", json={"messages": [{"role": "user", "content": "Question jamais posée"}]}
        )
        assert no_thread_yet.json() == {"pending": False}

        await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ?"}], "stream": False},
        )

        # Empty content on the last message: reproduces the real case
        # where the client (Open WebUI) sends back empty content for the
        # approval message despite its correct on-screen display.
        during_pause = await client.post(
            "/pending",
            json={
                "messages": [
                    {"role": "user", "content": "Question ?"},
                    {"role": "assistant", "content": ""},
                ]
            },
        )
        assert during_pause.json()["pending"] is True
        assert "Approbation requise" in during_pause.json()["text"]

        approved = await client.post(
            "/approve",
            json={
                "messages": [
                    {"role": "user", "content": "Question ?"},
                    {"role": "assistant", "content": ""},
                ],
                "approved": True,
            },
        )
        assert approved.status_code == 200


async def _stream_contents(client, messages):
    contents = []
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "agent-llm", "messages": messages, "stream": True},
    ) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: ") and "[DONE]" not in line:
                import json as _json

                payload = _json.loads(line[len("data: "):])
                delta = payload["choices"][0]["delta"]
                if delta.get("content"):
                    contents.append(delta["content"])
    return "".join(contents)


@pytest.mark.asyncio
async def test_streaming_endpoint_hides_tool_call_iteration_then_asks_approval(mock_side_services):
    """
    The iteration where the LLM decides to call a tool must produce no
    normal content token; only the approval message appears, all at
    once, in place of the final answer.
    """
    import app.graph as g
    import app.main as main_mod

    mock_side_services.post("http://fake-vllm/v1/chat/completions").mock(
        return_value=_sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}'))
    )
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        content = await _stream_contents(client, [{"role": "user", "content": "Question ?"}])

    assert "Approbation requise" in content
    assert mcp_route.call_count == 0


@pytest.mark.asyncio
async def test_streaming_endpoint_resumes_after_approval_reply(mock_side_services):
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        approval_text = await _stream_contents(client, [{"role": "user", "content": "Question ? Site : http://example.com"}])
        final_text = await _stream_contents(
            client,
            [
                {"role": "user", "content": "Question ? Site : http://example.com"},
                {"role": "assistant", "content": approval_text},
                {"role": "user", "content": "approuver"},
            ],
        )

    assert mcp_route.call_count == 1
    assert final_text == "Resultat: 42."


@pytest.mark.asyncio
async def test_streaming_endpoint_reopens_think_tag_after_approval_resume(mock_side_services):
    """
    Non-regression: the first turn reasons then requests a non-
    auto-approved tool -> pause. The orphaned </think> that then closes
    the approval message (closing_prefix, app/main.py) was never
    reflected in AgentState.think_opened/think_closed persisted by the
    checkpointer. Once the user approves and a SECOND reasoning round
    starts (before the final answer), the persisted state believed
    <think> was still open: no opening tag was re-emitted for this new
    reasoning, while a closing tag was indeed emitted at the end of the
    turn — a </think> visible on the client side with no matching
    <think> in this turn.
    """
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(
            reasoning_tool_call_response(["Je cherche."], "browser_navigate", "call_1", '{"url": "http://example.com"}')
        ),
        _sse_response(reasoning_response(["Je formule la réponse."], ["Resultat", ": 42."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        approval_text = await _stream_contents(client, [{"role": "user", "content": "Question ? Site : http://example.com"}])
        final_text = await _stream_contents(
            client,
            [
                {"role": "user", "content": "Question ? Site : http://example.com"},
                {"role": "assistant", "content": approval_text},
                {"role": "user", "content": "approuver"},
            ],
        )

    assert mcp_route.call_count == 1
    assert final_text.count("<think>") == final_text.count("</think>") == 1
    assert final_text.startswith("<think>")


@pytest.mark.asyncio
async def test_non_streaming_endpoint_forwards_worker_id_to_mcp_client(mock_side_services):
    """Effort 1.3 (docs/briefs/effort-1.3-parallel-campaigns.md): a
    parallel campaign worker's identity, sent as ChatCompletionRequest.worker_id,
    must reach mcp-client's /call via config["configurable"] — otherwise
    Phase 1's session isolation has nothing to key on from a real HTTP
    request, only from a hand-built graph config in a unit test."""
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("read_file", "call_1", '{"path": "/workspace/x.txt"}')),
        _sse_response(text_response(["Voil", "à."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "contenu"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "agent-llm",
                "messages": [{"role": "user", "content": "Lis le fichier"}],
                "stream": False,
                "worker_id": "worker-3",
            },
        )

    assert resp.status_code == 200
    sent = json.loads(mcp_route.calls.last.request.content)
    assert sent["worker_id"] == "worker-3"


@pytest.mark.asyncio
async def test_approve_endpoint_forwards_worker_id_to_mcp_client(mock_side_services):
    """Same as test_non_streaming_endpoint_forwards_worker_id_to_mcp_client,
    for the /approve resume path — a worker's approval follow-up must
    resolve on ITS OWN worker-scoped session, not the shared default one."""
    import app.graph as g
    import app.main as main_mod

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
        _sse_response(text_response(["Resultat", ": 42."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "42"}]})
    )
    g.agent_graph = g.build_graph()
    main_mod.agent_graph = g.agent_graph

    transport = httpx.ASGITransport(app=main_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/chat/completions",
            json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ? Site : http://example.com"}], "stream": False},
        )
        approval_text = first.json()["choices"][0]["message"]["content"]

        approved = await client.post(
            "/approve",
            json={
                "messages": [
                    {"role": "user", "content": "Question ? Site : http://example.com"},
                    {"role": "assistant", "content": approval_text},
                ],
                "approved": True,
                "worker_id": "worker-3",
            },
        )

    assert approved.status_code == 200
    sent = json.loads(mcp_route.calls.last.request.content)
    assert sent["worker_id"] == "worker-3"
