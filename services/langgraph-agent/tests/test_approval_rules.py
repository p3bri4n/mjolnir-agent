"""
Tests des règles sur arguments (Phase 4, app/approval_policy.py) : matchers
unitaires + effective_tier(), et un test d'intégration au niveau du graphe
pour vérifier le routage réel (has_tool_calls/require_approval).
"""

import httpx
import pytest
import respx

import app.approval_policy as policy
from tests.fixtures.llm_sse import text_response, tool_call_response


def test_load_rules_from_yaml(tmp_path):
    yaml_path = tmp_path / "rules.yaml"
    yaml_path.write_text(
        """
rules:
  - tool: custom_tool
    matcher: any
    tier: reversible
  - tool: custom_shell_tool
    matcher: command_prefix
    prefixes: ["ls", "git status"]
    tier: read
"""
    )
    rules = policy._load_rules_from_yaml(str(yaml_path))
    assert len(rules) == 2
    assert rules[0].tool == "custom_tool"
    assert rules[0].tier == policy.TIER_REVERSIBLE
    assert rules[0].matcher({}) is True
    assert rules[1].matcher({"command": "git status"}) is True
    assert rules[1].matcher({"command": "rm -rf /"}) is False


def test_approval_rules_path_env_extends_default_rules(monkeypatch, tmp_path):
    """DEFAULT_RULES is empty (no default argument rule today, see
    approval_policy.py) — APPROVAL_RULES_PATH only ever ADDS rules, never
    removes the (empty) default set."""
    yaml_path = tmp_path / "rules.yaml"
    yaml_path.write_text(
        """
rules:
  - tool: custom_tool
    matcher: any
    tier: reversible
"""
    )
    monkeypatch.setenv("APPROVAL_RULES_PATH", str(yaml_path))
    rules = policy._load_rules()
    assert len(rules) == len(policy.DEFAULT_RULES) + 1
    assert any(r.tool == "custom_tool" for r in rules)


def test_no_matching_rule_falls_back_to_static_tier():
    """read_file n'a aucune règle : effective_tier retombe sur tool_tier()."""
    assert policy.effective_tier("read_file", {}) == policy.TIER_READ


def test_rule_does_not_affect_other_tools(monkeypatch):
    """Une règle nommée pour un outil ne matche aucun autre outil."""
    monkeypatch.setattr(policy, "RULES", [policy.Rule("some_tool", policy._matcher_any, policy.TIER_REVERSIBLE)])
    assert policy.effective_tier("some_other_tool", {}) == policy.TIER_SENSITIVE


def test_command_prefix_matcher_factory():
    matcher = policy._matcher_command_prefix(["ls", "git status"])
    assert matcher({"command": "ls"})
    assert matcher({"command": "ls -la"})
    assert matcher({"command": "git status"})
    assert not matcher({"command": "rm -rf /"})


def test_custom_rule_overrides_static_tier_in_either_direction(monkeypatch):
    """
    Une règle peut aussi bien assouplir (sensible -> reversible) que durcir
    (read/reversible -> sensible) le tier statique d'un outil : les règles
    priment sur les tiers, pas seulement dans le sens le plus permissif.
    """
    monkeypatch.setattr(
        policy,
        "RULES",
        [policy.Rule("read_file", policy._matcher_any, policy.TIER_SENSITIVE)],
    )
    assert policy.effective_tier("read_file", {}) == policy.TIER_SENSITIVE


def test_ambiguous_matching_rules_pick_the_most_restrictive(monkeypatch):
    """Si plusieurs règles nommées pour le même outil matchent à la fois, la plus restrictive gagne."""
    monkeypatch.setattr(
        policy,
        "RULES",
        [
            policy.Rule("custom_tool", policy._matcher_any, policy.TIER_REVERSIBLE),
            policy.Rule("custom_tool", policy._matcher_any, policy.TIER_SENSITIVE),
        ],
    )
    # les deux règles matchent : reversible ET sensible -> sensible gagne
    assert policy.effective_tier("custom_tool", {}) == policy.TIER_SENSITIVE


def test_session_grant_still_applies_after_rule_resolution(monkeypatch):
    """Un grant de session peut rattraper un tier SENSIBLE résolu par une règle explicite."""
    monkeypatch.setattr(
        policy,
        "RULES",
        [policy.Rule("browser_navigate", policy._matcher_any, policy.TIER_SENSITIVE)],
    )
    assert policy.effective_tier("browser_navigate", {}, session_grants=["browser_navigate"]) == policy.TIER_REVERSIBLE


@pytest.mark.parametrize("tool_name", ["browser_run_code_unsafe", "browser_evaluate"])
def test_never_grantable_tools_stay_sensitive_despite_session_grant(tool_name):
    """Phase 1d-révisée (voir docs/history.md, T5) : exécution de code arbitraire
    dans la page est une élévation, pas une primitive de lecture — un grant
    de session ne doit JAMAIS l'assouplir, contrairement au reste des outils
    TIER_SENSITIVE (voir NEVER_GRANTABLE_TOOLS)."""
    assert policy.effective_tier(tool_name, {}, session_grants=[tool_name]) == policy.TIER_SENSITIVE


def test_never_grantable_tools_extra_blocks_relaxation(monkeypatch):
    """NEVER_GRANTABLE_TOOLS_EXTRA (docs/briefs/B3-benchmark-v2.md, famille
    B) : un outil ajouté par ce mécanisme doit se comporter EXACTEMENT
    comme les deux outils du set de base — jamais assoupli par un grant,
    même si un autre outil (hors de l'ensemble étendu) reste assouplissable."""
    monkeypatch.setattr(policy, "NEVER_GRANTABLE_TOOLS", policy.NEVER_GRANTABLE_TOOLS | {"browser_click"})
    assert policy.effective_tier("browser_click", {}, session_grants=["browser_click"]) == policy.TIER_SENSITIVE
    # Contrôle : un autre outil TIER_SENSITIVE non ajouté reste assouplissable.
    assert policy.effective_tier("browser_navigate", {}, session_grants=["browser_navigate"]) == policy.TIER_REVERSIBLE


# ─────────────────────────────────────────────────────────────────────────
# Test d'intégration : routage réel dans le graphe
# ─────────────────────────────────────────────────────────────────────────


def _sse_response(body):
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


CONFIG = {"configurable": {"thread_id": "test-thread-rules"}}


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
async def test_rule_relaxed_tool_auto_approved_in_graph(mock_side_services, monkeypatch):
    """No default argument rule exists today (DEFAULT_RULES is empty, see
    approval_policy.py) — this exercises the mechanism end-to-end via a
    RULES override, the same shape an APPROVAL_RULES_PATH deployment
    would produce."""
    import app.approval_policy as policy
    import app.graph as g

    monkeypatch.setattr(
        policy, "RULES", [policy.Rule("browser_evaluate", policy._matcher_any, policy.TIER_REVERSIBLE)]
    )

    route = mock_side_services.post("http://fake-vllm/v1/chat/completions")
    route.side_effect = [
        _sse_response(tool_call_response("browser_evaluate", "call_1", '{"code": "1+1"}')),
        _sse_response(text_response(["Fait", "."])),
    ]
    mcp_route = mock_side_services.post("http://fake-mcp-client/call").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    g.agent_graph = g.build_graph()

    state = {"messages": [{"role": "user", "content": "Évalue 1+1"}], "tool_iterations": 0, "approved": None}
    result = await g.agent_graph.ainvoke(state, CONFIG)

    snapshot = await g.agent_graph.aget_state(CONFIG)
    assert snapshot.next == ()  # aucune pause : assoupli par la règle
    assert mcp_route.call_count == 1
    assert result["messages"][-1].content == "Fait."
