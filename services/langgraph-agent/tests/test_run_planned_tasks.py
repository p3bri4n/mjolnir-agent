"""
tests_integration/test_web_tasks.py:_run_planned_tasks (effort 1.3, docs/
briefs/effort-1.3-parallel-campaigns.md): the N-worker execution loop
shared by _run_campaign (v1) and _run_campaign_v2 — covers ONLY the pure
concurrency/bookkeeping/pause/download-serialization logic, with
run_task/_purge_downloads_volume/_reset_browser_session monkeypatched to
fakes. Never touches Docker/HTTP, unlike test_web_tasks.py's own test
functions (opt-in RUN_LIVE_AGENT_TESTS=1) — vit dans tests/ (suite
rapide) for the same reason as test_campaign_persistence.py/
test_campaign_preflight.py.
"""

import threading
import time

import pytest

from tests_integration import campaign_persistence as cp
from tests_integration.test_web_tasks import TaskResult, _run_planned_tasks


def _state(planned, completed=None):
    return {"planned": planned, "completed": completed or [], "active": [], "current": None}


def _tasks_by_id(task_ids):
    # (task_id, base_prompt, assert_fn) — assert_fn unused by
    # _run_planned_tasks itself (build_row's job), only base_prompt is read.
    return {t: (t, f"prompt for {t}", None) for t in task_ids}


def _fake_paths(tmp_path):
    progress_path = tmp_path / "cid.progress.json"
    json_path = tmp_path / "cid.json"
    pause_path = tmp_path / "cid.pause"
    return progress_path, json_path, pause_path


def _ok_result(task_id):
    r = TaskResult()
    r.final_text = f"done: {task_id}"
    r.thread_id = f"thread-{task_id}"
    return r


def _build_row_factory(calls):
    def build_row(task_id, rep, prompt, thread_id, worker_id, result):
        calls.append((task_id, rep, worker_id))
        row = {"task_id": task_id, "repetition": rep, "duration_seconds": 1.0}
        completed_entry = {"task_id": task_id, "repetition": rep}
        return row, completed_entry

    return build_row


@pytest.fixture(autouse=True)
def _patch_web_tasks(monkeypatch):
    """Every test controls run_task itself (a per-test fake) — this
    fixture only neutralizes the two purge functions used when a test
    doesn't care about them, and campaign_persistence.append_campaign_row
    (real file I/O, irrelevant to this module's own logic, already
    covered by test_campaign_persistence.py)."""
    import tests_integration.test_web_tasks as wt

    monkeypatch.setattr(wt, "_purge_downloads_volume", lambda: None)
    monkeypatch.setattr(wt, "_reset_browser_session", lambda worker_id=None: None)
    monkeypatch.setattr(cp, "append_campaign_row", lambda *a, **k: None)
    yield


def test_sequential_default_preserves_order_and_worker_id_none(monkeypatch, tmp_path):
    import tests_integration.test_web_tasks as wt

    planned = [
        {"task_id": "T1", "repetition": 1},
        {"task_id": "T1", "repetition": 2},
        {"task_id": "T2", "repetition": 1},
    ]
    state = _state(planned)
    progress_path, json_path, pause_path = _fake_paths(tmp_path)
    seen_worker_ids = []

    def fake_run_task(prompt, worker_id=None):
        seen_worker_ids.append(worker_id)
        return _ok_result(prompt)

    monkeypatch.setattr(wt, "run_task", fake_run_task)
    calls = []
    rows, paused = _run_planned_tasks(
        state, _tasks_by_id(["T1", "T2"]), progress_path, json_path, {}, "2026-08-11T00:00:00Z",
        0, pause_path, _build_row_factory(calls), n_workers=1,
    )

    assert paused is False
    assert [(c[0], c[1]) for c in calls] == [("T1", 1), ("T1", 2), ("T2", 1)]
    assert seen_worker_ids == [None, None, None]
    assert len(rows) == 3
    assert [c["task_id"] for c in state["completed"]] == ["T1", "T1", "T2"]
    assert state["current"] is None
    assert state["active"] == []


def test_n_workers_claims_every_planned_entry_exactly_once(monkeypatch, tmp_path):
    import tests_integration.test_web_tasks as wt

    planned = [{"task_id": "T1", "repetition": r} for r in range(1, 11)]
    state = _state(planned)
    progress_path, json_path, pause_path = _fake_paths(tmp_path)

    def fake_run_task(prompt, worker_id=None):
        time.sleep(0.005)  # widen the window for real interleaving to occur
        return _ok_result(prompt)

    monkeypatch.setattr(wt, "run_task", fake_run_task)
    calls = []
    rows, paused = _run_planned_tasks(
        state, _tasks_by_id(["T1"]), progress_path, json_path, {}, "2026-08-11T00:00:00Z",
        0, pause_path, _build_row_factory(calls), n_workers=4,
    )

    assert paused is False
    assert sorted((c[1]) for c in calls) == list(range(1, 11))  # every repetition claimed exactly once
    assert len(rows) == 10
    assert len({c[2] for c in calls}) > 1  # more than one worker actually participated
    assert state["active"] == []


def test_missing_worker_id_default_bucket_semantics_unchanged(monkeypatch, tmp_path):
    """n_workers=1 must be byte-for-byte the pre-effort-1.3 behavior: no
    worker_id ever reaches run_task/reset — both see None, exactly what a
    caller that never opts in sees."""
    import tests_integration.test_web_tasks as wt

    planned = [{"task_id": "T1", "repetition": 1}]
    state = _state(planned)
    progress_path, json_path, pause_path = _fake_paths(tmp_path)
    reset_calls = []

    monkeypatch.setattr(wt, "_reset_browser_session", lambda worker_id=None: reset_calls.append(worker_id))
    monkeypatch.setattr(wt, "run_task", lambda prompt, worker_id=None: _ok_result(prompt))

    _run_planned_tasks(
        state, _tasks_by_id(["T1"]), progress_path, json_path, {}, "2026-08-11T00:00:00Z",
        0, pause_path, _build_row_factory([]), n_workers=1,
    )

    assert reset_calls == [None]


def test_pause_sentinel_stops_new_claims_but_finishes_in_flight(monkeypatch, tmp_path):
    """Effort 1.3: a pause request must not replay/skip work under N
    workers — new claims stop, whatever's already running finishes and
    gets persisted, paused=True is reported once every worker has
    actually stopped (not merely requested)."""
    import tests_integration.test_web_tasks as wt

    planned = [{"task_id": "T1", "repetition": r} for r in range(1, 6)]
    state = _state(planned)
    progress_path, json_path, pause_path = _fake_paths(tmp_path)
    claimed_lock = threading.Lock()
    claimed_count = {"n": 0}

    def fake_run_task(prompt, worker_id=None):
        with claimed_lock:
            claimed_count["n"] += 1
            n = claimed_count["n"]
        if n == 1:
            # First claimed run creates the pause sentinel WHILE running,
            # simulating `run-campaign.sh --pause` firing mid-campaign.
            pause_path.write_text("", encoding="utf-8")
        time.sleep(0.01)
        return _ok_result(prompt)

    monkeypatch.setattr(wt, "run_task", fake_run_task)
    calls = []
    rows, paused = _run_planned_tasks(
        state, _tasks_by_id(["T1"]), progress_path, json_path, {}, "2026-08-11T00:00:00Z",
        0, pause_path, _build_row_factory(calls), n_workers=1,
    )

    assert paused is True
    # Single worker: claims one, sees the pause sentinel written during
    # its OWN run, completes that one run, then stops — never claims a
    # second entry after noticing the pause.
    assert len(calls) == 1
    assert len(rows) == 1
    assert len(state["completed"]) == 1


def test_download_touching_task_blocks_other_workers_purge_until_it_finishes(monkeypatch, tmp_path):
    """Effort 1.3's downloads-volume design decision: a task tagged
    serialized_task_ids holds the purge lock for its WHOLE run, not just
    its own purge — another worker's purge (any task) must wait for it to
    fully finish, never interleave with its in-flight download."""
    import tests_integration.test_web_tasks as wt

    planned = [{"task_id": "T5", "repetition": 1}, {"task_id": "OTHER", "repetition": 1}]
    state = _state(planned)
    progress_path, json_path, pause_path = _fake_paths(tmp_path)
    events = []
    events_lock = threading.Lock()
    release_t5 = threading.Event()

    def log(name):
        with events_lock:
            events.append(name)

    def fake_purge():
        log("purge")

    def fake_run_task(prompt, worker_id=None):
        if "T5" in prompt:
            log("t5_run_start")
            release_t5.wait(timeout=2)
            log("t5_run_end")
        else:
            log("other_run")
        return _ok_result(prompt)

    monkeypatch.setattr(wt, "run_task", fake_run_task)

    def worker_thread_target():
        _run_planned_tasks(
            state, _tasks_by_id(["T5", "OTHER"]), progress_path, json_path, {}, "2026-08-11T00:00:00Z",
            0, pause_path, _build_row_factory([]), n_workers=2,
            purge_fns=(fake_purge,), serialized_task_ids={"T5"},
        )

    t = threading.Thread(target=worker_thread_target)
    t.start()
    # Let T5's worker reach its run_task (holding the lock) before
    # releasing it — deterministic enough via a short, generous wait.
    deadline = time.monotonic() + 2
    while "t5_run_start" not in events and time.monotonic() < deadline:
        time.sleep(0.005)
    assert "t5_run_start" in events, "T5 never started — test setup issue, not the code under test"
    time.sleep(0.05)  # give OTHER's worker every chance to (wrongly) slip its purge in here
    release_t5.set()
    t.join(timeout=5)

    # T5 is queued first (queue.pop(0)), so the two "purge" calls are
    # deterministically ordered: T5's own purge first, OTHER's second —
    # UNLESS the lock failed to hold, in which case OTHER's purge could
    # slip in between t5_run_start and t5_run_end. Assert directly on
    # that: OTHER's purge (the 2nd of exactly 2) must land after
    # t5_run_end, never inside T5's critical section.
    assert events.count("purge") == 2
    purge_indices = [i for i, e in enumerate(events) if e == "purge"]
    assert purge_indices[1] > events.index("t5_run_end")
