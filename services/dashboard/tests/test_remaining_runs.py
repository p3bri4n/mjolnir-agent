"""
app.main._remaining_runs/_compute_remaining_eta (effort 1.3, docs/briefs/
effort-1.3-parallel-campaigns.md): duplicate of
tests_integration/campaign_persistence.py's remaining_runs/
compute_remaining_eta, kept in sync manually per this service's own
"harness writes, dashboard reads" decoupling (separate image, no access
to that package). Same test cases as
services/langgraph-agent/tests/test_campaign_persistence.py's
remaining_runs tests, mirrored here so a future edit to one without the
other is caught locally, not just noticed by inspection.
"""

_PLANNED_T1_T1_T2 = [
    {"task_id": "T1", "repetition": 1},
    {"task_id": "T1", "repetition": 2},
    {"task_id": "T2", "repetition": 1},
]


def test_remaining_runs_matches_ordered_slice_when_completions_are_in_order():
    import app.main as main_mod

    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T1", "repetition": 1}]}
    assert main_mod._remaining_runs(state) == [
        {"task_id": "T1", "repetition": 2},
        {"task_id": "T2", "repetition": 1},
    ]


def test_remaining_runs_correct_when_completions_are_out_of_order():
    """A parallel campaign worker can finish a LATER planned entry before
    an earlier one — the dashboard's live ETA must not double-count or
    drop runs when that happens."""
    import app.main as main_mod

    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T2", "repetition": 1}]}
    assert main_mod._remaining_runs(state) == [
        {"task_id": "T1", "repetition": 1},
        {"task_id": "T1", "repetition": 2},
    ]


def test_compute_remaining_eta_correct_with_out_of_order_completions():
    import app.main as main_mod

    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T2", "repetition": 1}]}
    estimates = {"T1": {"median": 5.0, "min": 5.0, "max": 5.0, "n": 1}, "T2": {"median": 7.0, "min": 7.0, "max": 7.0, "n": 1}}
    eta = main_mod._compute_remaining_eta(state, estimates)
    assert eta["remaining_runs"] == 2
    assert eta["median_seconds"] == 10.0
