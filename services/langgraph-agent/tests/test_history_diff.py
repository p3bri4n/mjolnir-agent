"""
Tests du diff d'historique (HISTORY_DIFF_ENABLED, docs/briefs/
scaffolding-optimisation.md, Effort 2) — voir app/graph.py,
_apply_history_diff/_diff_browser_observation/_browser_result_indices.
Filtre transitoire, même principe que la rétention d'images
(test_image_retention_and_thinking.py) et la compaction d'épisode
(test_episode_compaction.py) : tests unitaires sur les fonctions pures
uniquement, jamais de docker/LLM réel ici.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _result_json(text: str) -> str:
    return json.dumps({"content": [{"type": "text", "text": text}]})


def _snapshot(url: str, affordances=()) -> str:
    lines = [f"Page URL: {url}"]
    for kind, label, target in affordances:
        lines.append(f'- {kind} "{label}"')
        if target:
            lines.append(f"  - /url: {target}")
    return "\n".join(lines)


def _browser_turn(tool_name="browser_snapshot", call_id="call_1", text="ok"):
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": tool_name, "args": {}}]),
        ToolMessage(content=_result_json(text), tool_call_id=call_id),
    ]


def _image_message(marker: str):
    return HumanMessage(content=[{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{marker}"}}])


def test_apply_history_diff_noop_when_disabled(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", False)
    messages = _browser_turn(call_id="c1") + _browser_turn(call_id="c2") + _browser_turn(call_id="c3")

    result = g._apply_history_diff(messages)

    assert result == messages


def test_apply_history_diff_noop_with_fewer_than_two_browser_results(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    messages = [HumanMessage(content="Objectif")] + _browser_turn(call_id="c1")

    result = g._apply_history_diff(messages)

    assert result == messages


def test_apply_history_diff_replaces_past_keeps_latest_raw(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(call_id="c1", text=_snapshot("http://catalog/page-1"))
    turn_b = _browser_turn(call_id="c2", text=_snapshot("http://catalog/page-2"))
    turn_c = _browser_turn(call_id="c3", text=_snapshot("http://catalog/page-3"))
    messages = turn_a + turn_b + turn_c

    result = g._apply_history_diff(messages)

    assert result[5] is messages[5], "le dernier résultat browser_* reste intact (même objet)"
    assert result[1].content.startswith(g._HISTORY_DIFF_MARKER)
    assert result[1].content != messages[1].content
    assert result[3].content.startswith(g._HISTORY_DIFF_MARKER)
    assert result[3].content != messages[3].content


def test_apply_history_diff_never_touches_original_messages_and_keeps_length(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    messages = (
        _browser_turn(call_id="c1", text=_snapshot("http://a/"))
        + _browser_turn(call_id="c2", text=_snapshot("http://b/"))
        + _browser_turn(call_id="c3", text=_snapshot("http://c/"))
    )
    snapshot = list(messages)

    result = g._apply_history_diff(messages)

    assert messages == snapshot
    assert result is not messages
    assert len(result) == len(messages)


def test_apply_history_diff_reports_url_change(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(call_id="c1", text=_snapshot("http://catalog/page-1"))
    turn_b = _browser_turn(call_id="c2", text=_snapshot("http://catalog/page-2"))
    turn_c = _browser_turn(call_id="c3", text=_snapshot("http://catalog/page-3"))
    messages = turn_a + turn_b + turn_c

    result = g._apply_history_diff(messages)

    assert "URL changée" in result[3].content  # turn_b's ToolMessage, diffed against turn_a
    assert "http://catalog/page-1" in result[3].content
    assert "http://catalog/page-2" in result[3].content


def test_apply_history_diff_reports_appeared_affordance(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(call_id="c1", text=_snapshot("http://catalog/", affordances=[("link", "Produit A", "/a")]))
    turn_b = _browser_turn(
        call_id="c2",
        text=_snapshot("http://catalog/", affordances=[("link", "Produit A", "/a"), ("link", "Produit B", "/b")]),
    )
    turn_c = _browser_turn(call_id="c3", text=_snapshot("http://catalog/"))
    messages = turn_a + turn_b + turn_c

    result = g._apply_history_diff(messages)

    assert "apparu" in result[3].content
    assert "Produit B" in result[3].content
    assert "Produit A" not in result[3].content.split("apparu")[1].split(";")[0]  # A n'a pas changé, pas listé


def test_apply_history_diff_reports_disappeared_affordance(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(
        call_id="c1",
        text=_snapshot("http://catalog/", affordances=[("link", "Produit A", "/a"), ("link", "Produit B", "/b")]),
    )
    turn_b = _browser_turn(call_id="c2", text=_snapshot("http://catalog/", affordances=[("link", "Produit A", "/a")]))
    turn_c = _browser_turn(call_id="c3", text=_snapshot("http://catalog/"))
    messages = turn_a + turn_b + turn_c

    result = g._apply_history_diff(messages)

    assert "disparu" in result[3].content
    assert "Produit B" in result[3].content


def test_apply_history_diff_first_observation_gets_fixed_marker(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(call_id="c1", text=_snapshot("http://catalog/page-1"))
    turn_b = _browser_turn(call_id="c2", text=_snapshot("http://catalog/page-2"))
    messages = turn_a + turn_b

    result = g._apply_history_diff(messages)

    assert "première observation" in result[1].content
    assert "URL changée" not in result[1].content


def test_apply_history_diff_non_structural_result_not_used_as_baseline(monkeypatch):
    """Séquence : snapshot réel A -> feedback de garde-fou (pas de page,
    ni URL ni affordance) -> snapshot réel B (pas le dernier) -> snapshot
    réel C (le dernier). Le feedback doit recevoir un marqueur neutre, ne
    jamais servir de base à un diff, et B doit être diffé contre A (pas
    contre le feedback bloqué, qui n'a pas d'URL)."""
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    turn_a = _browser_turn(call_id="c1", text=_snapshot("http://catalog/page-1"))
    turn_blocked = _browser_turn(call_id="c2", text="Navigation refusée : URL non observée sur cette page.")
    turn_b = _browser_turn(call_id="c3", text=_snapshot("http://catalog/page-2"))
    turn_c = _browser_turn(call_id="c4", text=_snapshot("http://catalog/page-3"))
    messages = turn_a + turn_blocked + turn_b + turn_c

    result = g._apply_history_diff(messages)

    blocked_content = result[3].content
    assert "action bloquée ou erreur" in blocked_content
    assert "première observation" not in blocked_content

    b_content = result[5].content
    assert "URL changée" in b_content
    assert "http://catalog/page-1" in b_content  # baseline = A, pas le feedback bloqué (pas d'URL "inconnue")
    assert "inconnue" not in b_content


def test_apply_history_diff_resolves_tool_call_id_in_mixed_turn(monkeypatch):
    """Un tour qui mélange un appel non-browser_* (ex. manage_plan) et un
    appel browser_* : seul le ToolMessage browser_* doit être détecté/
    remplacé."""
    import app.graph as g

    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)
    mixed_turn = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "plan_1", "name": "manage_plan", "args": {}},
                {"id": "c1", "name": "browser_snapshot", "args": {}},
            ],
        ),
        ToolMessage(content=_result_json("plan mis à jour"), tool_call_id="plan_1"),
        ToolMessage(content=_result_json(_snapshot("http://catalog/page-1")), tool_call_id="c1"),
    ]
    turn_latest = _browser_turn(call_id="c2", text=_snapshot("http://catalog/page-2"))
    messages = mixed_turn + turn_latest

    indices = g._browser_result_indices(messages)

    assert indices == [2, 4]  # seul le ToolMessage browser_* du 1er tour, + celui du 2e tour

    result = g._apply_history_diff(messages)
    assert result[1].content == messages[1].content  # manage_plan jamais touché
    assert result[2].content.startswith(g._HISTORY_DIFF_MARKER)


def test_browser_result_indices_counts_only_browser_messages(monkeypatch):
    import app.graph as g

    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "c1", "name": "browser_navigate", "args": {}},
                {"id": "p1", "name": "manage_plan", "args": {}},
            ],
        ),
        ToolMessage(content=_result_json("ok"), tool_call_id="c1"),
        ToolMessage(content=_result_json("ok"), tool_call_id="p1"),
        HumanMessage(content="texte quelconque"),
    ] + _browser_turn(call_id="c2")

    indices = g._browser_result_indices(messages)

    assert indices == [1, 5]


def test_apply_history_diff_composes_with_image_retention_and_episode_compaction(monkeypatch):
    """Reproduit l'ordre réel de call_llm : compaction d'épisode puis
    rétention d'images puis diff d'historique — aucun des trois filtres ne
    doit planter ni contaminer les messages gérés par un autre, y compris
    quand un message browser_* a déjà disparu (compacté) avant que le diff
    ne s'exécute."""
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    monkeypatch.setattr(g, "MAX_IMAGES_IN_CONTEXT", 1)
    monkeypatch.setattr(g, "HISTORY_DIFF_ENABLED", True)

    objective = HumanMessage(content="Trouve le prix")
    turn1 = _browser_turn(call_id="c1", text=_snapshot("http://catalog/page-1"))  # sous-tâche 0, compactée
    turn2 = _browser_turn(call_id="c2", text=_snapshot("http://catalog/page-2"))  # sous-tâche 1 (active), dès ici
    img1 = _image_message("aaa")
    turn3 = _browser_turn(call_id="c3", text=_snapshot("http://catalog/page-3"))
    img2 = _image_message("bbb")
    turn4 = _browser_turn(call_id="c4", text=_snapshot("http://catalog/page-4"))  # latest

    messages = [objective] + turn1 + turn2 + [img1] + turn3 + [img2] + turn4
    plan = [
        {"description": "Ouvrir", "success_criterion": "x", "status": "fait", "result": "ok"},
        {"description": "Lire le prix", "success_criterion": "y", "status": "en_cours", "result": None},
    ]
    boundaries = [1, 1 + len(turn1)]  # sous-tâche 0 = turn1 ; sous-tâche 1 (active) démarre à turn2

    compacted = g._apply_episode_compaction(messages, plan, boundaries)
    retained = g._apply_image_retention(compacted)
    diffed = g._apply_history_diff(retained)

    # turn1 a disparu (compacté) : plus de crash, plus d'index périmé.
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "c1" for m in diffed)
    # img1 remplacée par le placeholder de rétention, jamais touchée par le diff.
    img1_replacement = next(m for m in diffed if getattr(m, "content", None) == g.IMAGE_RETENTION_PLACEHOLDER)
    assert img1_replacement is not None
    # img2 reste une vraie image, intacte.
    assert any(g._is_image_message(m) for m in diffed)
    # turn2 (plus le prédécesseur structurel le plus proche, puisque turn1 a disparu) devient "première observation".
    turn2_tool_msg = next(m for m in diffed if isinstance(m, ToolMessage) and m.tool_call_id == "c2")
    assert "première observation" in turn2_tool_msg.content
    # turn3 (passé, structurel) est bien diffé contre turn2, pas contre "rien".
    turn3_tool_msg = next(m for m in diffed if isinstance(m, ToolMessage) and m.tool_call_id == "c3")
    assert "URL changée" in turn3_tool_msg.content
    # turn4 (le dernier) reste brut.
    turn4_tool_msg = next(m for m in diffed if isinstance(m, ToolMessage) and m.tool_call_id == "c4")
    assert turn4_tool_msg.content == _result_json(_snapshot("http://catalog/page-4"))
