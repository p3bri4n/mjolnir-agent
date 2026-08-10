"""
Replanification, routage post-vérification et rapport d'échec honnête
(Itération 2, Phase 1 « cœur cognitif » — voir
docs/briefs/phase-1-coeur-cognitif.md et app/graph.py:replan_task/
route_after_verification/report_failure).
"""

import json

import httpx
import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage

from tests.fixtures.llm_sse import non_streaming_response


class _FakeConfig(dict):
    """Minimal config expected by replan_task (thread_id, see app/audit_log.py) —
    same pattern as tests/test_verify_action.py."""

    def __init__(self, thread_id="thread-1"):
        super().__init__(configurable={"thread_id": thread_id})


def _subtask(description="A", success_criterion="critère A", status="a_faire", attempts=0, result=None):
    return {
        "description": description,
        "success_criterion": success_criterion,
        "status": status,
        "attempts": attempts,
        "result": result,
    }


# ─────────────────────────────────────────────────────────────────────────
# route_after_verification (pure)
# ─────────────────────────────────────────────────────────────────────────


def test_route_after_verification_continue_when_no_failed_subtask():
    """
    Depuis le correctif latence (Itération 4) : route_after_verification
    tourne APRÈS call_llm et délègue à has_tool_calls quand aucune
    sous-tâche n'est "echoue" (fusion de l'ancien "continue" avec le
    dispatch call_tools/auto_call_tools/retry_empty_answer/end — voir
    app/graph.py). Ici, réponse finale sans tool_calls -> "end".
    """
    import app.graph as g

    state = {
        "plan": [_subtask(status="fait"), _subtask(status="en_cours")],
        "messages": [AIMessage(content="Réponse finale.")],
        "tool_iterations": 0,
        "empty_answer_retries": 0,
    }
    assert g.route_after_verification(state) == "end"


def test_route_after_tool_execution_replan_under_budget():
    """Correctif latence 1/2-bis (voir docs/history.md) : le dispatch
    replan/give_up sur sous-tâche "echoue" a été déplacé de
    route_after_verification vers route_after_tool_execution (tourne APRÈS
    exécution des tool_calls, pour que report_and_act ait toujours son
    ToolMessage de reçu avant qu'on quitte cette branche du graphe)."""
    import app.graph as g
    from langchain_core.messages import AIMessage

    state = {
        "plan": [_subtask(status="echoue")],
        "replan_count": 0,
        "messages": [AIMessage(content="", tool_calls=[{"id": "r1", "name": "report_and_act", "args": {}}])],
    }
    assert g.route_after_tool_execution(state) == "replan"


def test_route_after_tool_execution_give_up_when_budget_exhausted(monkeypatch):
    import app.graph as g
    from langchain_core.messages import AIMessage

    monkeypatch.setattr(g, "REPLAN_BUDGET", 2)
    state = {
        "plan": [_subtask(status="echoue")],
        "replan_count": 2,
        "messages": [AIMessage(content="", tool_calls=[{"id": "r1", "name": "report_and_act", "args": {}}])],
    }
    assert g.route_after_tool_execution(state) == "give_up"


def test_route_after_verification_continue_with_empty_plan():
    import app.graph as g

    state = {
        "plan": [],
        "messages": [AIMessage(content="Réponse finale.")],
        "tool_iterations": 0,
        "empty_answer_retries": 0,
    }
    assert g.route_after_verification(state) == "end"


# ─────────────────────────────────────────────────────────────────────────
# replan_task
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replan_task_rebuilds_plan_preserving_done_subtasks(monkeypatch):
    import app.graph as g

    new_plan_json = json.dumps(
        {"sous_taches": [{"description": "Nouvelle approche", "critere_succes": "trouvé autrement"}]}
    )
    plan = [
        _subtask(description="Déjà fait", status="fait", result="ok"),
        _subtask(description="Échouée", status="echoue", attempts=3, result="rien trouvé"),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=non_streaming_response(new_plan_json))
        )
        state = {
            "messages": [HumanMessage(content="Trouve le produit")],
            "plan": plan,
            "replan_count": 0,
        }
        result = await g.replan_task(state, _FakeConfig())

    new_plan = result["plan"]
    assert result["replan_count"] == 1
    assert new_plan[0]["description"] == "Déjà fait"
    assert new_plan[0]["status"] == "fait"
    assert new_plan[1]["description"] == "Nouvelle approche"
    assert new_plan[1]["status"] == "en_cours"
    assert new_plan[1]["attempts"] == 0
    # Episode compaction (Phase 2, PLAN.md): the replanned subtask (index 1,
    # replacing the "echoue" one) gets a fresh boundary; the preserved
    # "fait" subtask (index 0) keeps none here (state had no prior
    # boundaries — see app/graph.py, AgentState.subtask_message_start).
    assert result["subtask_message_start"] == [len(state["messages"])]


@pytest.mark.asyncio
async def test_replan_task_falls_back_to_retry_on_llm_error(monkeypatch):
    import app.graph as g

    plan = [_subtask(description="Échouée", status="echoue", attempts=3, result="rien trouvé")]
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://fake-vllm/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
        state = {
            "messages": [HumanMessage(content="Trouve le produit")],
            "plan": plan,
            "replan_count": 0,
        }
        result = await g.replan_task(state, _FakeConfig())

    new_plan = result["plan"]
    assert result["replan_count"] == 1
    assert new_plan[0]["status"] == "en_cours"
    assert new_plan[0]["attempts"] == 0
    assert result["subtask_message_start"] == [len(state["messages"])]


@pytest.mark.asyncio
async def test_replan_task_noop_without_failed_subtask():
    import app.graph as g

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("http://fake-vllm/v1/chat/completions")
        state = {
            "messages": [HumanMessage(content="Trouve le produit")],
            "plan": [_subtask(status="fait")],
            "replan_count": 0,
        }
        result = await g.replan_task(state, _FakeConfig())

    assert result == {"replan_count": 1}
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_replan_task_logs_replanning_audit_entry_for_coverage(monkeypatch, tmp_path):
    """Coverage counter (EFFORT 2 "judge validity check", docs/history.md):
    replan_task now journalise a role="replanning" entry for every REAL
    replan (a failed subtask found) — the defensive no-op above (no
    failed subtask) stays unlogged, same convention as plan_task/
    validate_plan."""
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    new_plan_json = json.dumps(
        {"sous_taches": [{"description": "Nouvelle approche", "critere_succes": "trouvé autrement"}]}
    )
    plan = [_subtask(description="Échouée", status="echoue", attempts=3, result="rien trouvé")]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=non_streaming_response(new_plan_json))
        )
        state = {
            "messages": [HumanMessage(content="Trouve le produit")],
            "plan": plan,
            "replan_count": 0,
        }
        await g.replan_task(state, _FakeConfig("thread-replan-cov"))

    entries = audit_log.read_entries("thread-replan-cov")
    replans = [e for e in entries if e.get("kind") == "message" and e.get("role") == "replanning"]
    assert len(replans) == 1
    # failed_subtask/new_subtasks (EFFORT 2.3 follow-up, #51): the literal
    # description/success_criterion of the subtask that stalled, plus the
    # replacement plan — needed to diagnose which criterion a stalled
    # subtask was judged against (previously only counts were logged).
    assert replans[0]["content"] == {
        "replan_index": 1,
        "failed_subtask_index": 0,
        "failed_subtask": {"description": "Échouée", "success_criterion": "critère A"},
        "new_subtask_count": 1,
        "new_subtasks": [{"description": "Nouvelle approche", "success_criterion": "trouvé autrement"}],
    }


@pytest.mark.asyncio
async def test_replan_task_no_audit_entry_without_failed_subtask(monkeypatch, tmp_path):
    """Symmetric negative case: the defensive early return (no subtask
    actually "echoue") consumes replan_count budget but changes nothing —
    must not be counted as a real replan in the coverage counter."""
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    state = {
        "messages": [HumanMessage(content="Trouve le produit")],
        "plan": [_subtask(status="fait")],
        "replan_count": 0,
    }
    await g.replan_task(state, _FakeConfig("thread-replan-noop"))

    entries = audit_log.read_entries("thread-replan-noop")
    replans = [e for e in entries if e.get("kind") == "message" and e.get("role") == "replanning"]
    assert replans == []


# ─────────────────────────────────────────────────────────────────────────
# Correctif d'ancrage (Itération 4) : replan_task inclut l'état de la page
# quand current_page_url est renseigné (Phase 1).
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replan_task_includes_page_snapshot_when_current_page_url_set():
    import app.graph as g

    new_plan_json = json.dumps(
        {"sous_taches": [{"description": "Nouvelle approche", "critere_succes": "trouvé autrement", "outils": []}]}
    )
    plan = [_subtask(description="Échouée", status="echoue", attempts=3, result="rien trouvé")]
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        mock.post("http://fake-mcp-client/call").mock(
            return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "pagination uniquement"}]})
        )
        llm_route = mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=non_streaming_response(new_plan_json))
        )
        state = {
            "messages": [HumanMessage(content="Trouve le produit")],
            "plan": plan,
            "replan_count": 0,
            "current_page_url": "http://fixture-catalog/catalog/page-2.html",
        }
        await g.replan_task(state, _FakeConfig())

    sent_content = llm_route.calls.last.request.content.decode()
    assert "pagination uniquement" in sent_content


# ─────────────────────────────────────────────────────────────────────────
# report_failure
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_failure_summarizes_plan_state():
    import app.graph as g

    plan = [
        _subtask(description="Ouvrir le catalogue", status="fait", result="ok"),
        _subtask(description="Trouver le produit", status="echoue", result="introuvable"),
    ]
    result = await g.report_failure({"plan": plan})

    text = result["messages"][0]["content"]
    assert "pas pu terminer" in text
    assert "[fait] Ouvrir le catalogue — ok" in text
    assert "[échoué] Trouver le produit — introuvable" in text
