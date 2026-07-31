"""
Non-régression : l'ajout du checkpointer + thread_id stable (supervision
humaine des outils) a introduit un risque de duplication des messages —
Open WebUI renvoie l'historique COMPLET à chaque requête, alors que ce
thread a déjà persisté les tours précédents. Sans le filtrage par
owui_message_count (app/main.py:_resolve_run), chaque nouveau tour
réinjectait tout l'historique déjà stocké, dupliquant les messages à chaque
tour (vérifié : 2 tours simples produisaient 6 messages internes au lieu de
4). Ces tests figent le comportement correct.

Défaut /approve (2026-07-31, voir docs/resolved-bugs.md) : /approve
calculait owui_message_count en supposant TOUJOURS la convention Open
WebUI (le bouton d'action ÉDITE en place le message "⚠️ Approbation
requise" — messages envoyé inclut donc déjà un emplacement pour ce tour,
voir test_tool_approval_then_new_turn_message_count ci-dessous). Un client
programmatique qui AJOUTE la réponse finale comme un nouveau message
plutôt que d'éditer en place (ex.
tests_integration/probe_compaction_multi_turn.py) envoie un `messages`
plus court d'un cran — sans détection, le tour suivant réinjectait le
contenu déjà répondu comme s'il était nouveau, découvert en multi-tours
avec approbation à chaque tour (aucun client multi-tours n'existait
avant). /approve détecte maintenant la convention en comparant
`len(request.messages)` au compte déjà persisté pour ce thread — voir les
tests `test_approve_append_convention_*` ci-dessous.
"""

import httpx
import pytest
import respx

from tests.fixtures.llm_sse import text_response, tool_call_response


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
async def test_two_turn_conversation_message_count():
    import app.graph as g
    import app.main as main_mod

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://fake-context-manager/retrieve").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        mock.post("http://fake-skill-manager/match").mock(
            return_value=httpx.Response(200, json={"skill": None})
        )
        mock.get("http://fake-mcp-client/tools/schema").mock(
            return_value=httpx.Response(200, json={"tools": []})
        )
        route = mock.post("http://fake-vllm/v1/chat/completions")
        route.side_effect = [
            _sse_response(text_response(["Bonjour", " !"])),
            _sse_response(text_response(["Comment", " puis-je", " aider ?"])),
        ]

        g.agent_graph = g.build_graph()
        main_mod.agent_graph = g.agent_graph

        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": [{"role": "user", "content": "Salut"}], "stream": False},
            )
            second = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-llm",
                    "messages": [
                        {"role": "user", "content": "Salut"},
                        {"role": "assistant", "content": first.json()["choices"][0]["message"]["content"]},
                        {"role": "user", "content": "Comment vas-tu ?"},
                    ],
                    "stream": False,
                },
            )

        thread_id = main_mod._derive_thread_id(
            [type("M", (), {"role": "user", "content": "Salut"})()]
        )
        snapshot = await g.agent_graph.aget_state({"configurable": {"thread_id": thread_id}})
        # human1, AI1, human2, AI2 : exactement 4, aucun doublon de tour 1
        assert len(snapshot.values["messages"]) == 4


@pytest.mark.asyncio
async def test_tool_approval_then_new_turn_message_count():
    import app.graph as g
    import app.main as main_mod

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
        route = mock.post("http://fake-vllm/v1/chat/completions")
        route.side_effect = [
            _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
            _sse_response(text_response(["Resultat", ": 42."])),
            _sse_response(text_response(["Autre", " reponse."])),
        ]

        g.agent_graph = g.build_graph()
        main_mod.agent_graph = g.agent_graph

        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ?"}], "stream": False},
            )
            approval_text = first.json()["choices"][0]["message"]["content"]

            second = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-llm",
                    "messages": [
                        {"role": "user", "content": "Question ?"},
                        {"role": "assistant", "content": approval_text},
                        {"role": "user", "content": "approuver"},
                    ],
                    "stream": False,
                },
            )
            final_text = second.json()["choices"][0]["message"]["content"]

            third = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-llm",
                    "messages": [
                        {"role": "user", "content": "Question ?"},
                        {"role": "assistant", "content": approval_text},
                        {"role": "user", "content": "approuver"},
                        {"role": "assistant", "content": final_text},
                        {"role": "user", "content": "Autre question ?"},
                    ],
                    "stream": False,
                },
            )

        thread_id = main_mod._derive_thread_id(
            [type("M", (), {"role": "user", "content": "Question ?"})()]
        )
        snapshot = await g.agent_graph.aget_state({"configurable": {"thread_id": thread_id}})
        # human1, AI(tool_call), tool, AI(final), human2, AI(autre) : exactement 6, aucun doublon
        assert len(snapshot.values["messages"]) == 6
        assert third.json()["choices"][0]["message"]["content"] == "Autre reponse."


@pytest.mark.asyncio
async def test_approve_append_convention_then_new_turn_message_count():
    """Convention B (voir docstring du module) : /approve reçoit `messages`
    SANS le message d'approbation en attente — le client l'ajoutera comme
    un nouveau message une fois cet appel résolu, jamais en éditant en
    place. Même invariant que le test ci-dessus (convention Open WebUI) :
    aucun contenu déjà répondu ne doit être réinjecté au tour suivant."""
    import app.graph as g
    import app.main as main_mod

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
        route = mock.post("http://fake-vllm/v1/chat/completions")
        route.side_effect = [
            _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://example.com"}')),
            _sse_response(text_response(["Resultat", ": 42."])),
            _sse_response(text_response(["Autre", " reponse."])),
        ]

        g.agent_graph = g.build_graph()
        main_mod.agent_graph = g.agent_graph

        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question ?"}], "stream": False},
            )

            # Pas de placeholder : convention B.
            approved = await client.post(
                "/approve",
                json={"messages": [{"role": "user", "content": "Question ?"}], "approved": True},
            )
            final_text = approved.json()["content"]
            assert final_text == "Resultat: 42."

            # Le client AJOUTE la vraie réponse finale comme nouveau message.
            third = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agent-llm",
                    "messages": [
                        {"role": "user", "content": "Question ?"},
                        {"role": "assistant", "content": final_text},
                        {"role": "user", "content": "Autre question ?"},
                    ],
                    "stream": False,
                },
            )

        thread_id = main_mod._derive_thread_id(
            [type("M", (), {"role": "user", "content": "Question ?"})()]
        )
        snapshot = await g.agent_graph.aget_state({"configurable": {"thread_id": thread_id}})
        # human1, AI(tool_call), tool, AI(final), human2, AI(autre) : exactement 6, comme la
        # convention Open WebUI — les deux conventions doivent converger vers le même invariant.
        assert len(snapshot.values["messages"]) == 6
        assert third.json()["choices"][0]["message"]["content"] == "Autre reponse."


@pytest.mark.asyncio
async def test_approve_append_convention_across_consecutive_approval_turns():
    """Cas qui a révélé le défaut en conditions réelles
    (tests_integration/probe_compaction_multi_turn.py) : plusieurs tours
    de suite nécessitant CHACUN une approbation (session_grants remis à
    zéro à chaque nouveau tour, voir _resolve_run) — sans le correctif,
    le déficit d'un cran s'ACCUMULE et réinjecte de plus en plus de
    contenu déjà répondu au fil des tours."""
    import app.graph as g
    import app.main as main_mod

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
        route = mock.post("http://fake-vllm/v1/chat/completions")
        route.side_effect = [
            _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://a.example"}')),
            _sse_response(text_response(["Premiere", " reponse."])),
            _sse_response(tool_call_response("browser_navigate", "call_2", '{"url": "http://b.example"}')),
            _sse_response(text_response(["Deuxieme", " reponse."])),
            _sse_response(text_response(["Troisieme", " reponse."])),
        ]

        g.agent_graph = g.build_graph()
        main_mod.agent_graph = g.agent_graph

        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": [{"role": "user", "content": "Question un ?"}], "stream": False},
            )
            approved1 = await client.post(
                "/approve",
                json={"messages": [{"role": "user", "content": "Question un ?"}], "approved": True},
            )
            final1 = approved1.json()["content"]
            assert final1 == "Premiere reponse."

            history_turn2 = [
                {"role": "user", "content": "Question un ?"},
                {"role": "assistant", "content": final1},
                {"role": "user", "content": "Question deux ?"},
            ]
            await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": history_turn2, "stream": False},
            )
            approved2 = await client.post("/approve", json={"messages": history_turn2, "approved": True})
            final2 = approved2.json()["content"]
            assert final2 == "Deuxieme reponse."

            history_turn3 = history_turn2 + [
                {"role": "assistant", "content": final2},
                {"role": "user", "content": "Question trois ?"},
            ]
            third = await client.post(
                "/v1/chat/completions",
                json={"model": "agent-llm", "messages": history_turn3, "stream": False},
            )

        thread_id = main_mod._derive_thread_id(
            [type("M", (), {"role": "user", "content": "Question un ?"})()]
        )
        snapshot = await g.agent_graph.aget_state({"configurable": {"thread_id": thread_id}})
        # 3 tours x (human, AI(tool_call), tool, AI(final)) sauf le dernier, texte
        # seul (human, AI) : 4 + 4 + 2 = 10, jamais de contenu réinjecté.
        assert len(snapshot.values["messages"]) == 10
        assert third.json()["choices"][0]["message"]["content"] == "Troisieme reponse."
