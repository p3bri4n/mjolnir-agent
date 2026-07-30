"""
Family F "verbatim" guarantee (docs/briefs/B3-benchmark-v2.md): T3/T5/T6/T10
must be the SAME wording/assertion as v1, checked here by identity against
test_web_tasks.py's TASKS rather than by eyeballing two copies of the same
text staying in sync (test_web_tasks_v2.py imports these tuples directly,
never re-declares them).
"""
import os

from tests_integration import test_web_tasks as v1
from tests_integration import test_web_tasks_v2 as v2


def test_family_f_task_ids_match_brief_order():
    assert v2.FAMILY_F_TASK_IDS == [
        "T3_tableau_dynamique",
        "T5_telechargement_calcul",
        "T6_session_authentifiee",
        "T10_books_toscrape",
    ]
    assert [t[0] for t in v2.FAMILY_F_TASKS] == v2.FAMILY_F_TASK_IDS


def test_family_f_tasks_are_the_same_objects_as_v1():
    v1_by_id = {t[0]: t for t in v1.TASKS}
    for task_id, prompt, assert_fn in v2.FAMILY_F_TASKS:
        v1_task_id, v1_prompt, v1_assert_fn = v1_by_id[task_id]
        assert prompt == v1_prompt
        assert assert_fn is v1_assert_fn  # same function object, not a re-implementation


def test_family_f_defaults_to_two_repetitions():
    # Only meaningful when the env override isn't set, same as the module
    # default it's asserting against (docs/briefs/B3-benchmark-v2.md: "2
    # repetitions instead of 3").
    if "WEB_TASKS_V2_REPETITIONS" not in os.environ:
        assert v2.N_REPETITIONS_V2_F == 2
    assert v2._repetitions_for_task("T3_tableau_dynamique") == v2.N_REPETITIONS_V2_F


# ─────────────────────────────────────────────────────────────────────────
# Family B (intent α only) — CuP core
# ─────────────────────────────────────────────────────────────────────────


def test_family_b_alpha_reuses_v1_t2_verbatim():
    v1_by_id = {t[0]: t for t in v1.TASKS}
    v1_task_id, v1_prompt, v1_assert_fn = v1_by_id["T2_formulaire_conge"]
    for task_id, prompt, assert_fn in v2.FAMILY_B_TASKS:
        assert prompt == v1_prompt
        assert assert_fn is v1_assert_fn


def test_family_b_defaults_to_three_repetitions():
    if "WEB_TASKS_V2_REPETITIONS_B" not in os.environ:
        assert v2.N_REPETITIONS_V2_DEFAULT == 3
    assert v2._repetitions_for_task("B1_conge_easy") == v2.N_REPETITIONS_V2_DEFAULT


def test_family_b_policies_escalate_by_load():
    # Each load's policy list must be a strict superset of the previous
    # one — "+" in the brief's table means ADDED, never swapped out.
    easy = set(v2.FAMILY_B_POLICIES["B1_conge_easy"])
    medium = set(v2.FAMILY_B_POLICIES["B1_conge_medium"])
    hard = set(v2.FAMILY_B_POLICIES["B1_conge_hard"])
    assert easy < medium < hard


def _entry(tool, tier="sensitive", url=None):
    args = {"url": url} if url is not None else {}
    return {"tool": tool, "tier": tier, "arguments": args}


def test_policy_domain_scope_passes_within_scope():
    entries = [_entry("browser_navigate", url="http://fixture-hr-app:5000/leave-form")]
    ok, detail = v2._policy_domain_scope(entries)
    assert ok is True
    assert detail == ""


def test_policy_domain_scope_fails_outside_scope():
    entries = [_entry("browser_navigate", url="http://evil.example/phish")]
    ok, detail = v2._policy_domain_scope(entries)
    assert ok is False
    assert "evil.example" in detail


def test_policy_no_grant_relaxation_passes_when_all_individual():
    entries = [_entry("browser_click", tier="sensitive")]
    ok, detail = v2._policy_no_grant_relaxation(entries)
    assert ok is True


def test_policy_no_grant_relaxation_fails_when_relaxed():
    entries = [_entry("browser_click", tier="reversible")]
    ok, detail = v2._policy_no_grant_relaxation(entries)
    assert ok is False
    assert "browser_click" in detail


def test_policy_no_never_grantable_tool_passes_when_absent():
    entries = [_entry("browser_navigate"), _entry("browser_click")]
    ok, detail = v2._policy_no_never_grantable_tool(entries)
    assert ok is True


def test_policy_no_never_grantable_tool_fails_when_used():
    entries = [_entry("browser_evaluate")]
    ok, detail = v2._policy_no_never_grantable_tool(entries)
    assert ok is False
    assert "browser_evaluate" in detail


def test_evaluate_policies_no_op_for_family_f(monkeypatch):
    called = []
    monkeypatch.setattr(v2, "_audit_entries", lambda tid: called.append(tid) or [])
    result = v2._evaluate_policies("T3_tableau_dynamique", "thread-x")
    assert result == {}
    assert called == []  # never even fetches the audit log for a non-policy task


def test_evaluate_policies_easy_load_only_checks_domain_scope(monkeypatch):
    monkeypatch.setattr(v2, "_audit_entries", lambda tid: [_entry("browser_navigate", url="http://fixture-hr-app:5000/x")])
    result = v2._evaluate_policies("B1_conge_easy", "thread-x")
    assert result["policy_load"] == "easy"
    assert result["policies_checked"] == ["domain_scope"]
    assert result["policy_violations"] == []


def test_evaluate_policies_hard_load_reports_all_violations(monkeypatch):
    monkeypatch.setattr(
        v2,
        "_audit_entries",
        lambda tid: [
            _entry("browser_navigate", url="http://evil.example/x"),
            _entry("browser_click", tier="reversible"),
            _entry("browser_evaluate"),
        ],
    )
    result = v2._evaluate_policies("B1_conge_hard", "thread-x")
    assert len(result["policy_violations"]) == 3


# ─────────────────────────────────────────────────────────────────────────
# Family D (honesty) — "heir of" v1 T7/T11, not "verbatim"
#
# D2 wraps _t11_task(), a REAL live HTTP fetch (python.org) — only
# _family_d_tasks()/_all_v2_tasks() trigger it, never a bare import of
# this module (see their docstrings). Deliberately NOT called here: same
# discipline as v1, which never unit-tests _t11_task() either (it's only
# exercised by the live, opt-in campaign harness).
# ─────────────────────────────────────────────────────────────────────────


def test_family_d_task_ids():
    assert v2.FAMILY_D_TASK_IDS == ["D1_cible_inexistante", "D2_sonde_peremption"]


def test_d1_reuses_v1_t7_verbatim():
    v1_by_id = {t[0]: t for t in v1.TASKS}
    _, v1_prompt, v1_assert_fn = v1_by_id["T7_impossible_par_construction"]
    assert v2._D1_PROMPT == v1_prompt
    assert v2._D1_ASSERT_FN is v1_assert_fn


def test_family_d_defaults_to_the_shared_repetitions_default():
    assert v2._repetitions_for_task("D1_cible_inexistante") == v2.N_REPETITIONS_V2_DEFAULT
    assert v2._repetitions_for_task("D2_sonde_peremption") == v2.N_REPETITIONS_V2_DEFAULT


def test_known_urls_by_task_v2_has_d1_entry():
    assert "D1_cible_inexistante" in v2.ALL_KNOWN_URLS_BY_TASK
    assert "D2_sonde_peremption" not in v2.ALL_KNOWN_URLS_BY_TASK  # real site, no sub-classification


def _fake_result(failure_cause=None):
    r = v1.TaskResult()
    r.failure_cause = failure_cause
    return r


def test_classify_failure_cause_v2_maps_generic_failure_to_hallucination_for_d1():
    cause = v2._classify_failure_cause_v2("D1_cible_inexistante", _fake_result(), False, "prix inventé")
    assert cause == "hallucination"


def test_classify_failure_cause_v2_maps_generic_failure_to_hallucination_for_d2():
    cause = v2._classify_failure_cause_v2("D2_sonde_peremption", _fake_result(), False, "mauvaise version")
    assert cause == "hallucination"


def test_classify_failure_cause_v2_leaves_other_tasks_unaffected():
    # Same task_id v1 already special-cases (T7's own literal id, not
    # D1) must still get v1's answer unchanged — the override is scoped
    # to the v2 ids only, never widens v1's own behavior.
    cause = v2._classify_failure_cause_v2("T7_impossible_par_construction", _fake_result(), False, "x")
    assert cause == "hallucination"  # v1's OWN special-case, not the v2 override
    cause2 = v2._classify_failure_cause_v2("T3_tableau_dynamique", _fake_result(), False, "x")
    assert cause2 == "extraction"  # generic fallback, never overridden for a non-family-D id


def test_classify_failure_cause_v2_passes_through_boucle_unchanged():
    cause = v2._classify_failure_cause_v2("D1_cible_inexistante", _fake_result("boucle"), False, "x")
    assert cause.startswith("boucle")  # never overridden to "hallucination"
