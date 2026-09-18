"""
Tests de la compaction d'épisode (EPISODE_COMPACTION_ENABLED/
EPISODE_COMPACTION_TURN_THRESHOLD, PLAN.md Phase 2) — voir app/graph.py,
_apply_episode_compaction/_summarize_subtask. Filtre transitoire, même
principe que la rétention d'images (test_image_retention_and_thinking.py) :
tests unitaires sur les fonctions pures uniquement, jamais de docker/LLM
réel ici.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _subtask(description="Ouvrir le catalogue", status="fait", result="prix trouvé"):
    return {"description": description, "success_criterion": "peu importe", "status": status, "result": result}


def _turn(tool_name="browser_navigate", arg_value="http://example/", call_id="call_1", result_text="ok"):
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": tool_name, "args": {"url": arg_value}}]),
        ToolMessage(content=result_text, tool_call_id=call_id),
    ]


def _result_json(text: str) -> str:
    return json.dumps({"content": [{"type": "text", "text": text}]})


def _snapshot(url: str, affordances=()) -> str:
    lines = [f"Page URL: {url}"]
    for kind, label in affordances:
        lines.append(f'- {kind} "{label}"')
    return "\n".join(lines)


def _structural_turn(url: str, affordances=(), call_id="call_1"):
    """Same shape as test_history_diff.py's _browser_turn — a REAL
    structural browser_* result (JSON content, parseable snapshot), as
    opposed to _turn()'s plain-string dummy content above."""
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": "browser_navigate", "args": {}}]),
        ToolMessage(content=_result_json(_snapshot(url, affordances)), tool_call_id=call_id),
    ]


def test_apply_episode_compaction_noop_when_disabled(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", False)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    messages = [HumanMessage(content="Objectif")] + _turn()
    plan = [_subtask()]

    result = g._apply_episode_compaction(messages, plan, [1])

    assert result == messages


def test_apply_episode_compaction_noop_under_threshold(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 1000)
    messages = [HumanMessage(content="Objectif")] + _turn()
    plan = [_subtask()]

    result = g._apply_episode_compaction(messages, plan, [1])

    assert result == messages


def test_apply_episode_compaction_replaces_completed_subtask_range(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    objective = HumanMessage(content="Trouve le prix")
    turn = _turn(arg_value="http://catalog/")
    messages = [objective] + turn
    plan = [_subtask(description="Ouvrir le catalogue", status="fait", result="prix trouvé")]

    result = g._apply_episode_compaction(messages, plan, [1])

    assert result[0] is objective, "l'objectif (avant le 1er boundary) ne doit jamais être compacté"
    assert len(result) == 2
    summary = result[1].content
    assert "Ouvrir le catalogue" in summary
    assert "browser_navigate" in summary
    assert "prix trouvé" in summary


def test_apply_episode_compaction_leaves_active_subtask_untouched(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    objective = HumanMessage(content="Trouve le prix")
    done_turn = _turn(call_id="call_1", arg_value="http://catalog/")
    active_turn = _turn(call_id="call_2", arg_value="http://catalog/page-2/")
    messages = [objective] + done_turn + active_turn
    plan = [
        _subtask(description="Ouvrir le catalogue", status="fait"),
        _subtask(description="Lire le prix", status="en_cours", result=None),
    ]
    boundaries = [1, 1 + len(done_turn)]

    result = g._apply_episode_compaction(messages, plan, boundaries)

    assert len(result) == 1 + 1 + len(active_turn)  # objectif + résumé + tour actif intact
    assert result[-2:] == active_turn


def test_apply_episode_compaction_never_touches_original_messages(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    messages = [HumanMessage(content="Objectif")] + _turn()
    snapshot = list(messages)
    plan = [_subtask()]

    g._apply_episode_compaction(messages, plan, [1])

    assert messages == snapshot


def test_apply_episode_compaction_degrades_gracefully_on_boundary_desync(monkeypatch):
    """Un plan/boundaries désynchronisés (ex. index hors bornes) ne doit
    jamais lever — juste ne rien compacter (voir docstring de la fonction)."""
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    messages = [HumanMessage(content="Objectif")] + _turn()
    plan = [_subtask(status="fait")]

    result = g._apply_episode_compaction(messages, plan, [])  # boundaries vide, désynchro avec plan

    assert result == messages


def test_apply_episode_compaction_skips_subtasks_not_yet_done(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "EPISODE_COMPACTION_ENABLED", True)
    monkeypatch.setattr(g, "EPISODE_COMPACTION_TURN_THRESHOLD", 0)
    messages = [HumanMessage(content="Objectif")] + _turn()
    plan = [_subtask(status="a_faire", result=None)]

    result = g._apply_episode_compaction(messages, plan, [1])

    assert result == messages


def test_summarize_subtask_includes_description_actions_and_result():
    import app.graph as g

    subtask = _subtask(description="Ouvrir le catalogue", result="prix trouvé : 84.90")
    turns = _turn(tool_name="browser_navigate", arg_value="http://catalog/")

    summary = g._summarize_subtask(subtask, turns)

    assert "Ouvrir le catalogue" in summary
    assert "browser_navigate" in summary
    assert "prix trouvé : 84.90" in summary


def test_summarize_subtask_handles_missing_result():
    import app.graph as g

    subtask = _subtask(description="Sous-tâche", result=None)

    summary = g._summarize_subtask(subtask, [])

    assert "résultat non consigné" in summary


def test_summarize_subtask_omits_anchor_without_structural_browser_result():
    """_turn()'s dummy content ("ok") isn't a real snapshot — must degrade
    to the narrative-only summary, never raise or fabricate an anchor."""
    import app.graph as g

    subtask = _subtask()
    turns = _turn()

    summary = g._summarize_subtask(subtask, turns)

    assert "état constaté" not in summary


def test_summarize_subtask_appends_state_anchor_from_real_browser_result():
    """The A4 fix: a REAL structural browser_* result in the subtask's
    turns must surface its URL/affordances as a factual anchor, separate
    from the narrative — this is exactly what the narrative-only version
    never captured (docs/engineering-log.md, "A4 / COMPACTION")."""
    import app.graph as g

    subtask = _subtask(description="Aller aux employés")
    turns = _structural_turn("http://fixture-hr-app:5000/employees", [("link", "Congés")])

    summary = g._summarize_subtask(subtask, turns)

    assert "état constaté en fin de sous-tâche" in summary
    assert "URL=http://fixture-hr-app:5000/employees" in summary
    assert 'link "Congés"' in summary


def test_subtask_state_anchor_empty_without_browser_tool_call():
    import app.graph as g

    turns = _turn(tool_name="read_file", result_text="contenu")

    assert g._subtask_state_anchor(turns) == ""


def test_subtask_state_anchor_empty_on_non_structural_result():
    """A guardrail/error feedback carries no page state — never used as a
    fabricated anchor (see _is_structural_browser_result)."""
    import app.graph as g

    turns = [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "browser_navigate", "args": {}}]),
        ToolMessage(content=_result_json("Navigation refusée : URL fabriquée."), tool_call_id="c1"),
    ]

    assert g._subtask_state_anchor(turns) == ""


def test_subtask_state_anchor_uses_the_last_browser_result():
    """Multiple browser_* calls in range: the anchor reflects where the
    subtask actually ENDED, not its first observation."""
    import app.graph as g

    turns = _structural_turn("http://catalog/page-1", call_id="c1") + _structural_turn(
        "http://catalog/page-2", call_id="c2"
    )

    anchor = g._subtask_state_anchor(turns)

    assert "http://catalog/page-2" in anchor
    assert "http://catalog/page-1" not in anchor
