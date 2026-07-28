"""
URL-fabrication guardrail (Phase 1, see PLAN.md/docs/history.md): finding
#1 of the Phase 0 point-zero (tests_integration/test_web_tasks.py, T1/T7)
— the agent regularly invents plausible URLs never observed (page-4.html
on a 3-page catalog, a nonexistent search path...) rather than following
a real DOM link.

`browser_navigate` now checks the requested URL against the set of
observed URLs (task-scope roots + navigations already executed + links
seen in a previous browser_* tool result) before calling mcp-client — a
never-observed URL is rejected WITHOUT execution, with explicit tool
feedback, and counted in `fabricated_navigation_attempts`.

"First hop" fix (browser-session reliability effort, see
docs/history.md): a task's very FIRST navigation (no URL observed yet,
`has_prior_navigation` false in `app/graph.py`) is now ALWAYS allowed,
even with no URL in the prompt — root cause found on real tasks with no
given URL (T8 "on Wikipedia...", T11 "what's the latest Python
version?") whose first navigation, though legitimate, was blocked as
fabrication. The fabrication actually observed (T1/T7) always occurs
AFTER exploration has already started, never as a first move: the
guardrail stays fully active from the SECOND navigation onward, whether
a URL was given or not.
"""

import httpx
import pytest
import respx

from tests.fixtures.llm_sse import text_response, tool_call_response


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


CONFIG = {"configurable": {"thread_id": "test-thread-url-guardrail"}}


@pytest.fixture
def mock_side_services():
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
        yield mock


@pytest.mark.asyncio
async def test_first_navigate_without_task_url_is_allowed(mock_side_services):
    """"First hop" fix (see docs/history.md): a real task with no URL in
    the prompt (e.g. T8/T11) must no longer have its FIRST navigation
    blocked as fabrication — nothing has been observed yet, no
    fabrication is possible on a very first starting choice."""
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "https://www.python.org/downloads/"}')),
        _sse_response(text_response(["Réponse", " finale."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(
            200, json={"content": [{"type": "text", "text": "Page URL: https://www.python.org/downloads/"}]}
        )
    )
    g.agent_graph = g.build_graph()

    state = {
        "messages": [{"role": "user", "content": "Quelle est la dernière version stable de Python ?"}],
        "tool_iterations": 0,
        "approved": None,
    }
    await g.agent_graph.ainvoke(state, CONFIG)
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    assert mcp_route.call_count == 1
    assert result["fabricated_navigation_attempts"] == 0


@pytest.mark.asyncio
async def test_second_fabricated_url_still_blocked_without_calling_mcp(mock_side_services):
    """Once AT LEAST one navigation has already happened (observed_urls
    non-empty at the start, simulated here), a subsequent never-observed
    URL stays rejected WITHOUT execution — the main protection
    (fabrication during ongoing exploration, T1/T7) stays fully intact
    after the "first hop" fix."""
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://invente.example/page-4.html"}')),
        _sse_response(text_response(["Réponse", " finale."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ne devrait jamais être appelé"}]})
    )
    g.agent_graph = g.build_graph()

    state = {
        "messages": [{"role": "user", "content": "Fais une recherche."}],
        "tool_iterations": 0,
        "approved": None,
        "observed_urls": ["http://exemple.com/deja-observee.html"],
    }
    await g.agent_graph.ainvoke(state, CONFIG)
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    assert mcp_route.call_count == 0
    tool_message = next(m for m in result["messages"] if getattr(m, "type", None) == "tool")
    assert "URL non observée" in tool_message.content
    assert result["fabricated_navigation_attempts"] == 1


@pytest.mark.asyncio
async def test_navigate_to_task_scope_url_is_allowed(mock_side_services):
    """The URL mentioned in the 1st human message (task scope) is
    allowed right away, without having been "observed" beforehand."""
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://fixture-catalog/catalog/index.html"}')),
        _sse_response(text_response(["Réponse", " finale."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "Page URL: http://fixture-catalog/catalog/index.html"}]})
    )
    g.agent_graph = g.build_graph()

    state = {
        "messages": [{"role": "user", "content": "Sur http://fixture-catalog/catalog/index.html, trouve le prix."}],
        "tool_iterations": 0,
        "approved": None,
    }
    await g.agent_graph.ainvoke(state, CONFIG)
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    assert mcp_route.call_count == 1
    assert result["fabricated_navigation_attempts"] == 0
    assert "http://fixture-catalog/catalog/index.html" in result["observed_urls"]


def test_extract_urls_from_snapshot_resolves_relative_links():
    """Relative links ("- /url: ...", Playwright snapshot format) are
    resolved to absolute via the current page before comparison."""
    import app.graph as g

    text = (
        "### Page\n- Page URL: http://fixture-catalog/catalog/page-2.html\n"
        "### Snapshot\n"
        '- link "Produit #14" [ref=e5]:\n  - /url: /catalog/product-14.html\n'
    )
    page_url = g._extract_page_url(text)
    assert page_url == "http://fixture-catalog/catalog/page-2.html"

    urls = g._extract_urls(text, page_url)
    assert "http://fixture-catalog/catalog/product-14.html" in urls
    assert "http://fixture-catalog/catalog/page-2.html" in urls


def test_truncate_browser_result_caps_text_length():
    import app.graph as g

    huge_text = "x" * 20000
    result = {"content": [{"type": "text", "text": huge_text}]}
    truncated = g._truncate_browser_result(result, max_chars=100)

    assert len(truncated["content"][0]["text"]) < len(huge_text)
    assert truncated["content"][0]["text"].startswith("x" * 100)
    assert "tronqué" in truncated["content"][0]["text"]


def test_truncate_browser_result_leaves_small_text_untouched():
    import app.graph as g

    result = {"content": [{"type": "text", "text": "court"}]}
    truncated = g._truncate_browser_result(result, max_chars=100)

    assert truncated == result


def _synthetic_long_catalog_page(n_products: int) -> str:
    """Simulates a real Playwright snapshot (see docs/history.md,
    "truncation starves navigation"): lots of descriptive content BEFORE
    the link list, like books.toscrape.com's category sidebar which,
    under real conditions, occupies exactly the first few thousand
    characters and pushes the product list past the truncation
    threshold."""
    filler = "\n".join(f"  - generic [ref=e{i}]: Description remplissage {i}" for i in range(400))
    links = "\n".join(
        f'    - link "Produit #{i}" [ref=p{i}] [cursor=pointer]:\n      - /url: /catalog/product-{i}.html'
        for i in range(n_products)
    )
    return f"### Page\n- Page URL: http://fixture-catalog/catalog/page-1.html\n### Snapshot\n```yaml\n{filler}\n{links}\n```"


def test_structured_truncation_preserves_all_links_below_affordance_threshold():
    """Explicit criterion (see docs/history.md): long catalog page but
    BELOW AFFORDANCE_THRESHOLD -> the truncated snapshot contains 100% of
    the links, despite a size cap largely exceeded by the raw text."""
    import app.graph as g

    n_products = min(40, g.AFFORDANCE_THRESHOLD - 1)
    text = _synthetic_long_catalog_page(n_products=n_products)
    assert len(text) > 8000  # confirms this case would indeed exceed the default cap

    all_links = g._extract_urls(text, "http://fixture-catalog/catalog/page-1.html")
    assert len(all_links) == n_products + 1  # + the page's own URL ("Page URL: ..." line)

    result = {"content": [{"type": "text", "text": text}]}
    truncated = g._truncate_browser_result(result, max_chars=2000)
    truncated_text = truncated["content"][0]["text"]

    assert len(truncated_text) < len(text)
    survived_links = g._extract_urls(truncated_text, "http://fixture-catalog/catalog/page-1.html")
    assert survived_links == all_links  # 100% of links, no loss despite truncation


def test_hierarchical_inventory_keeps_pagination_and_relevant_content_on_huge_page():
    """Explicit criterion (Phase 1d, point 2): a 500-link page -> the
    truncated snapshot contains the pagination AND the content relevant
    to the task's objective — see docs/history.md, T8 archive check (593
    affordances on a real Wikipedia page starved all content, including
    the semantic link "Naissance" -> "Muret")."""
    import app.graph as g

    filler = "\n".join(f"  - generic [ref=e{i}]: Bruit {i}" for i in range(50))
    nav_links = (
        '    - link "Suivant" [ref=n1]:\n      - /url: /catalog/page-2.html\n'
        '    - link "Précédent" [ref=n2]:\n      - /url: /catalog/page-0.html\n'
    )
    content_links = "\n".join(
        f'    - link "Produit générique #{i}" [ref=c{i}]:\n      - /url: /catalog/product-{i}.html'
        for i in range(500)
    )
    target_link = (
        '    - link "Article recherché KX-4471" [ref=target]:\n      - /url: /catalog/product-target.html\n'
    )
    text = (
        f"### Page\n- Page URL: http://fixture-catalog/catalog/page-1.html\n### Snapshot\n```yaml\n"
        f"{filler}\n{nav_links}\n{target_link}\n{content_links}\n```"
    )

    structured = g._extract_affordances_structured(text)
    assert len(structured) > g.AFFORDANCE_THRESHOLD

    result = {"content": [{"type": "text", "text": text}]}
    truncated = g._truncate_browser_result(result, max_chars=4000, objective="trouve l'article KX-4471")
    truncated_text = truncated["content"][0]["text"]

    assert "/catalog/page-2.html" in truncated_text  # "Next" pagination always kept whole
    assert "/catalog/page-0.html" in truncated_text  # "Previous" pagination always kept whole
    assert "/catalog/product-target.html" in truncated_text  # content relevant to the objective, prioritized
    assert "liens de contenu supplémentaires" in truncated_text  # the rest is counted, not listed


def test_extract_affordances_pairs_labels_with_urls_and_lists_buttons_without():
    import app.graph as g

    text = (
        '- link "Voir le catalogue" [ref=e1]:\n  - /url: /catalog/page-1.html\n'
        '- button "Rechercher" [ref=e2]\n'
    )
    affordances = g._extract_affordances(text)
    assert '- link "Voir le catalogue" /url: /catalog/page-1.html' in affordances
    assert '- button "Rechercher"' in affordances


def test_fabrication_feedback_tier1_is_minimal_without_link_list():
    """Rejections 1-2 (see docs/history.md, Phase 1c): minimal message, NO
    list — the structured snapshot already contains the full link
    inventory (see _extract_affordances), re-supplying it on every
    rejection was the real cause of the 1b regression."""
    import app.graph as g

    for attempt in (1, 2):
        page_links = ["http://fixture-catalog/catalog/page-1.html"]
        feedback = g._fabrication_feedback("http://invente.example/page-4.html", attempt, page_links)
        assert "http://fixture-catalog/catalog/page-1.html" not in feedback
        assert "URL non observée" in feedback


def test_fabrication_feedback_tier2_includes_closest_links():
    """Rejection 3 (and up to FABRICATION_LIMIT-1): a few links closest to
    the fabricated URL, not a full directory."""
    import app.graph as g

    available = [
        "http://fixture-catalog/catalog/product-14.html",
        "http://fixture-catalog/catalog/page-2.html",
        "http://fixture-catalog/catalog/product-4471-x.html",
    ]
    feedback = g._fabrication_feedback("http://fixture-catalog/catalog/product-4471.html", 3, available)
    assert "tentative n°3" in feedback
    assert "http://fixture-catalog/catalog/product-4471-x.html" in feedback  # closest to the fabricated match


def test_fabrication_feedback_at_limit_always_concludes_absence():
    """At the cap (FABRICATION_LIMIT, default 5): unconditional message
    (Phase 1c) — pushes toward an honest absence conclusion rather than
    yet another guess (bridge to T7). A conditional redirect toward
    "strong candidates" was attempted in Phase 1d then SUSPENDED (see
    docs/history.md, T5/T8 archive check): the hypothesis motivating this
    branch wasn't supported by the observed sequences — the real T5 fix
    lives on the infra side (dedicated download volume), not in this
    feedback."""
    import app.graph as g

    available = ["http://exemple.com/reel.html"]
    feedback = g._fabrication_feedback("http://invente.example/x.html", g.FABRICATION_LIMIT, available)
    assert "introuvable" in feedback
    assert "réponse valide" in feedback
    assert "http://exemple.com/reel.html" not in feedback


@pytest.mark.asyncio
async def test_fabrication_attempt_number_wired_from_state_counter(mock_side_services):
    """Verifies the end-to-end wiring (not just _fabrication_feedback in
    isolation): the task's 1st REJECTION must receive the tier 1
    (minimal) message, consistent with fabricated_navigation_attempts=0
    at the start. Simulates an already-performed navigation
    (observed_urls non-empty, see "first hop" fix) so that THIS
    navigation is indeed the first to be evaluated by the guardrail, not
    the task's very first one (now always allowed)."""
    import app.graph as g

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_navigate", "call_1", '{"url": "http://invente.example/page-4.html"}')),
        _sse_response(text_response(["Réponse", " finale."])),
    ]
    mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ne devrait jamais être appelé"}]})
    )
    g.agent_graph = g.build_graph()

    state = {
        "messages": [{"role": "user", "content": "Trouve un prix."}],
        "tool_iterations": 0,
        "approved": None,
        "observed_urls": ["http://exemple.com/deja-observee.html"],
    }
    await g.agent_graph.ainvoke(state, CONFIG)
    await g.agent_graph.aupdate_state(CONFIG, {"approved": True})
    result = await g.agent_graph.ainvoke(None, CONFIG)

    tool_message = next(m for m in result["messages"] if getattr(m, "type", None) == "tool")
    assert "tentative n°" not in tool_message.content  # tier 1: no number displayed, minimal message
    assert result["fabricated_navigation_attempts"] == 1
