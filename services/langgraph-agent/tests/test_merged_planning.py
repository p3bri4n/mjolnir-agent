"""
Merged-planning mode (EFFORT 2 point 3, PLANNING_MODE="merged" — see
docs/briefs/update-plan.md "2.1 addendum" and app/graph.py's manage_plan
dispatch/_get_bound_llm/_merged_plan_directive). The 5th cognitive-core
condition: planning as a synthetic tool call in the main turn instead of
a dedicated node with its own LLM call. PLANNING_MODE defaults to "nodes"
(current 4-flag behavior, unchanged) — every test here exercises "merged"
explicitly via monkeypatch, same pattern as PLANNER_ENABLED
(tests/test_plan_task.py).
"""

import json

import httpx
import pytest
import respx
from langchain_core.messages import AIMessage, HumanMessage

from tests.fixtures.llm_sse import text_response

CONFIG = {"configurable": {"thread_id": "test-thread-merged-planning"}}


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _manage_plan_call(action, call_id="call_1", **extra_args):
    return {"id": call_id, "name": "manage_plan", "args": {"action": action, **extra_args}}


def _state_with_tool_call(tool_call, plan=None):
    return {
        "messages": [
            HumanMessage(content="Trouve le prix du produit et vérifie sa disponibilité"),
            AIMessage(content="", tool_calls=[tool_call]),
        ],
        "plan": plan or [],
        "tool_iterations": 0,
        "session_grants": [],
    }


# ─────────────────────────────────────────────────────────────────────────
# approval_policy.tool_tier
# ─────────────────────────────────────────────────────────────────────────


def test_manage_plan_tool_is_tier_read():
    import app.approval_policy as approval_policy

    assert approval_policy.tool_tier(approval_policy.MANAGE_PLAN_TOOL_NAME) == approval_policy.TIER_READ


# ─────────────────────────────────────────────────────────────────────────
# _get_bound_llm: manage_plan only exposed when PLANNING_MODE == "merged"
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manage_plan_tool_exposed_when_planning_mode_merged(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        llm_route = mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=_sse_response(text_response(["OK"]))
        )
        state = {"messages": [HumanMessage(content="Salut")], "tool_iterations": 0}
        await g.call_llm(state, CONFIG)

    sent_tools = json.loads(llm_route.calls.last.request.content)["tools"]
    assert [t["function"]["name"] for t in sent_tools] == ["manage_plan"]


@pytest.mark.asyncio
async def test_manage_plan_tool_absent_when_planning_mode_nodes(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "PLANNING_MODE", "nodes")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        llm_route = mock.post("http://fake-vllm/v1/chat/completions").mock(
            return_value=_sse_response(text_response(["OK"]))
        )
        state = {"messages": [HumanMessage(content="Salut")], "tool_iterations": 0}
        await g.call_llm(state, CONFIG)

    sent_body = json.loads(llm_route.calls.last.request.content)
    assert "tools" not in sent_body or sent_body["tools"] == []


# ─────────────────────────────────────────────────────────────────────────
# _execute_tool_calls: manage_plan(set_plan) — accepted
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_plan_accepted_creates_plan_and_logs_coverage(monkeypatch, tmp_path):
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    subtasks = [
        {"description": "Ouvrir le catalogue", "success_criterion": "page affichée"},
        {"description": "Lire le prix", "success_criterion": "prix trouvé"},
    ]
    tool_call = _manage_plan_call("set_plan", subtasks=subtasks)
    state = _state_with_tool_call(tool_call)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        result = await g._execute_tool_calls(state, {"configurable": {"thread_id": "thread-set-plan-ok"}})

    assert len(result["plan"]) == 2
    assert result["plan"][0]["status"] == "en_cours"
    assert result["plan"][1]["status"] == "a_faire"
    assert result["subtask_message_start"] == [len(state["messages"])]
    tool_message = next(m for m in result["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["ok"] is True

    entries = audit_log.read_entries("thread-set-plan-ok")
    merged_entries = [e for e in entries if e.get("role") == "merged_planning"]
    assert len(merged_entries) == 1
    assert merged_entries[0]["content"] == {
        "action": "set_plan",
        "subtask_count": 2,
        "heuristic_rejected": False,
        "subtask_index": None,
    }


# ─────────────────────────────────────────────────────────────────────────
# _execute_tool_calls: manage_plan(set_plan) — rejected by heuristics
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_plan_rejected_by_heuristics_never_mutates_plan(monkeypatch, tmp_path):
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    # Below plan_validation.SUBTASKS_MIN (2): a single subtask is rejected.
    tool_call = _manage_plan_call(
        "set_plan", subtasks=[{"description": "Une seule sous-tâche", "success_criterion": "X"}]
    )
    state = _state_with_tool_call(tool_call, plan=[])

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        result = await g._execute_tool_calls(state, {"configurable": {"thread_id": "thread-set-plan-reject"}})

    assert "plan" not in result  # plan state untouched — no key at all, not even []
    tool_message = next(m for m in result["messages"] if m.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert "error" in payload
    assert any("bornes" in reason for reason in payload["reasons"])

    entries = audit_log.read_entries("thread-set-plan-reject")
    merged_entries = [e for e in entries if e.get("role") == "merged_planning"]
    assert merged_entries[0]["content"]["heuristic_rejected"] is True


@pytest.mark.asyncio
async def test_set_plan_replan_replaces_remaining_subtasks(monkeypatch, tmp_path):
    """A stuck subtask is handled by calling set_plan again — never a
    persisted "echoue" status (which would route to the costly
    replan_task node in the 4-flag architecture)."""
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    existing_plan = [
        {"description": "Ouvrir le catalogue", "success_criterion": "page affichée", "status": "fait", "attempts": 0, "result": None},
        {"description": "Lire le prix", "success_criterion": "prix trouvé", "status": "en_cours", "attempts": 2, "result": None},
    ]
    new_subtasks = [
        {"description": "Chercher le prix ailleurs", "success_criterion": "prix trouvé sur une autre page"},
        {"description": "Confirmer", "success_criterion": "confirmé"},
    ]
    tool_call = _manage_plan_call("set_plan", subtasks=new_subtasks)
    state = _state_with_tool_call(tool_call, plan=existing_plan)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://fake-mcp-client/tools/schema").mock(return_value=httpx.Response(200, json={"tools": []}))
        result = await g._execute_tool_calls(state, {"configurable": {"thread_id": "thread-replan"}})

    assert len(result["plan"]) == 2
    assert result["plan"][0]["description"] == "Chercher le prix ailleurs"
    assert result["plan"][0]["status"] == "en_cours"
    assert not any(st.get("status") == "echoue" for st in result["plan"])


# ─────────────────────────────────────────────────────────────────────────
# _execute_tool_calls: manage_plan(complete_subtask)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_subtask_advances_active_index(monkeypatch, tmp_path):
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    plan = [
        {"description": "A", "success_criterion": "critère A", "status": "en_cours", "attempts": 0, "result": None},
        {"description": "B", "success_criterion": "critère B", "status": "a_faire", "attempts": 0, "result": None},
    ]
    tool_call = _manage_plan_call("complete_subtask", subtask_index=0)
    state = _state_with_tool_call(tool_call, plan=plan)

    with respx.mock(assert_all_called=False) as mock:
        result = await g._execute_tool_calls(state, {"configurable": {"thread_id": "thread-complete"}})

    assert result["plan"][0]["status"] == "fait"
    assert result["plan"][1]["status"] == "en_cours"
    tool_message = next(m for m in result["messages"] if m.get("role") == "tool")
    assert json.loads(tool_message["content"])["ok"] is True


@pytest.mark.asyncio
async def test_complete_subtask_invalid_index_rejected(monkeypatch, tmp_path):
    import app.graph as g
    import app.audit_log as audit_log

    monkeypatch.setattr(audit_log, "AUDIT_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    plan = [{"description": "A", "success_criterion": "critère A", "status": "a_faire", "attempts": 0, "result": None}]
    tool_call = _manage_plan_call("complete_subtask", subtask_index=0)  # status is "a_faire", not "en_cours"
    state = _state_with_tool_call(tool_call, plan=plan)

    with respx.mock(assert_all_called=False) as mock:
        result = await g._execute_tool_calls(state, {"configurable": {"thread_id": "thread-complete-invalid"}})

    assert "plan" not in result
    tool_message = next(m for m in result["messages"] if m.get("role") == "tool")
    assert "error" in json.loads(tool_message["content"])


# ─────────────────────────────────────────────────────────────────────────
# _merged_plan_directive
# ─────────────────────────────────────────────────────────────────────────


def test_merged_plan_directive_empty_outside_merged_mode(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "PLANNING_MODE", "nodes")
    assert g._merged_plan_directive({"plan": []}) == ""


def test_merged_plan_directive_prompts_set_plan_before_any_plan(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    directive = g._merged_plan_directive({"plan": []})
    assert "set_plan" in directive


def test_merged_plan_directive_states_active_subtask(monkeypatch):
    import app.graph as g

    monkeypatch.setattr(g, "PLANNING_MODE", "merged")
    plan = [{"description": "Ouvrir X", "success_criterion": "X ouvert", "status": "en_cours", "attempts": 0, "result": None}]
    directive = g._merged_plan_directive({"plan": plan})
    assert "Ouvrir X" in directive
    assert "X ouvert" in directive
