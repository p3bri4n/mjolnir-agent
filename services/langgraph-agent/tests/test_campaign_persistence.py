"""
tests_integration/campaign_persistence.py : couvre la logique PURE
(agrégation, génération d'id, écriture/lecture JSON) sans docker/git réel
(monkeypatch sur `_run`/`subprocess.run`, même esprit que
test_campaign_preflight.py) — vit dans tests/ précisément parce que cette
logique n'a pas besoin de la stack live pour être vérifiée.
"""

from datetime import datetime, timezone

import pytest

from tests_integration import campaign_persistence as cp


# ─────────────────────────────────────────────────────────────────────────
# Collecte de métadonnées (subprocess mocké : jamais de docker/git réel ici)
# ─────────────────────────────────────────────────────────────────────────


def test_run_returns_none_on_nonzero_exit(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp._run(["git", "rev-parse", "HEAD"]) is None


def test_run_returns_none_on_timeout(monkeypatch):
    def _raise(*a, **k):
        raise cp.subprocess.TimeoutExpired(cmd="x", timeout=1)

    monkeypatch.setattr(cp.subprocess, "run", _raise)
    assert cp._run(["docker", "info"]) is None


def test_git_commit_returns_stripped_output(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "abc123\n"

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp.git_commit() == "abc123"


def test_docker_image_id_best_effort_on_missing_container(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp.docker_image_id("does-not-exist") is None


def test_collect_image_digests_maps_each_container(monkeypatch):
    monkeypatch.setattr(cp, "docker_image_id", lambda name: f"sha256:{name}")
    digests = cp.collect_image_digests(["a", "b"])
    assert digests == {"a": "sha256:a", "b": "sha256:b"}


def test_collect_env_flags_filters_to_known_keys(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "MAX_TOOL_ITERATIONS=40\nPATH=/usr/bin\nVERIFICATION_ENABLED=true\n"

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    flags = cp.collect_env_flags(flags=["MAX_TOOL_ITERATIONS", "VERIFICATION_ENABLED"])
    assert flags == {"MAX_TOOL_ITERATIONS": "40", "VERIFICATION_ENABLED": "true"}
    assert "PATH" not in flags


def test_collect_metadata_merges_mcp_client_env_flags(monkeypatch):
    """CAMPAIGN_VISUAL_CAPTURE (docs/briefs/campaign-visual-feedback.md)
    lives on mcp-client, not langgraph-agent — collect_metadata must query
    BOTH containers and merge into one flat env_flags dict. fake_run must
    handle every _run() call collect_metadata makes (git rev-parse, docker
    inspect ×4 containers, docker exec tabbyapi python3, docker exec env
    ×2) — matched by shape, not just "exec", since several of those also
    contain "exec" at a different position."""

    class _Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        if len(args) >= 4 and args[0] == "docker" and args[1] == "exec" and args[-1] == "env":
            container = args[2]
            if container == cp.MCP_CLIENT_CONTAINER:
                return _Result("CAMPAIGN_VISUAL_CAPTURE=true\nPATH=/usr/bin\n")
            return _Result("PLANNER_ENABLED=true\n")
        return _Result("")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    metadata = cp.collect_metadata("Campagne test")

    assert metadata["env_flags"]["PLANNER_ENABLED"] == "true"
    assert metadata["env_flags"]["CAMPAIGN_VISUAL_CAPTURE"] == "true"


def test_campaign_env_flags_includes_planning_mode():
    """CAMPAIGN_ENV_FLAGS (drives what's persisted to a campaign's archived
    env_flags) and EXPECTED_AGENT_FLAGS (campaign_preflight.py, drives the
    pre-run assertion) are two separate lists — PLANNING_MODE was added to
    the latter (EFFORT 2 point 3) but not the former, so every merged-mode
    campaign ran with a verified-correct PLANNING_MODE that its own
    archived JSON couldn't show (docs/resolved-bugs.md, fifth-condition
    correction 1/2 follow-up). Regression guard."""
    assert "PLANNING_MODE" in cp.CAMPAIGN_ENV_FLAGS


def test_collect_env_flags_empty_dict_when_container_unreachable(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp.collect_env_flags() == {}


def test_fetch_tabbyapi_model_id_parses_output(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "agent-llm\n"

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp.fetch_tabbyapi_model_id() == "agent-llm"


def test_collect_metadata_never_raises_when_everything_unreachable(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    metadata = cp.collect_metadata("Campagne test")
    assert metadata["commit"] is None
    assert metadata["tabbyapi_model_id"] is None
    assert metadata["env_flags"] == {}
    assert metadata["label"] == "Campagne test"
    assert set(metadata["image_ids"]) == set(cp.CAMPAIGN_IMAGE_CONTAINERS)
    assert metadata["gpu_devices"] == []


# ─────────────────────────────────────────────────────────────────────────
# GPU devices (docs/briefs/deterministic-gpu-placement.md, step 5)
# ─────────────────────────────────────────────────────────────────────────

_GPU_SMI_CSV = (
    "0, NVIDIA GeForce RTX 5060 Ti, 6052, 00000000:04:00.0\n"
    "1, NVIDIA GeForce RTX 4070 Ti SUPER, 12616, 00000000:08:00.0\n"
)


def test_collect_gpu_devices_parses_csv_output(monkeypatch):
    class _Result:
        returncode = 0
        stdout = _GPU_SMI_CSV

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    devices = cp.collect_gpu_devices()
    assert devices == [
        {"index": 0, "name": "NVIDIA GeForce RTX 5060 Ti", "memory_used_mib": 6052.0, "bus_id": "00000000:04:00.0"},
        {
            "index": 1,
            "name": "NVIDIA GeForce RTX 4070 Ti SUPER",
            "memory_used_mib": 12616.0,
            "bus_id": "00000000:08:00.0",
        },
    ]


def test_collect_gpu_devices_empty_on_docker_failure(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    assert cp.collect_gpu_devices() == []


def test_collect_metadata_includes_gpu_devices(monkeypatch):
    class _Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(args, **kwargs):
        if len(args) >= 4 and args[0] == "docker" and args[1] == "exec" and args[3] == "nvidia-smi":
            return _Result(_GPU_SMI_CSV)
        return _Result("")

    monkeypatch.setattr(cp.subprocess, "run", fake_run)
    metadata = cp.collect_metadata("Campagne test")
    assert len(metadata["gpu_devices"]) == 2
    assert metadata["gpu_devices"][1]["name"] == "NVIDIA GeForce RTX 4070 Ti SUPER"


# ─────────────────────────────────────────────────────────────────────────
# Échantillons TabbyAPI bruts (regex sur docker logs) + agrégat dérivé
# ─────────────────────────────────────────────────────────────────────────

_SAMPLE_LOG_LINE = (
    "12 tokens generated in 0.5 seconds (Queue: 0.0 s, Process: 100 cached tokens "
    "and 50 new tokens at 200.0 T/s)"
)


def test_collect_tabbyapi_raw_samples_parses_each_request(monkeypatch):
    class _Result:
        returncode = 0
        stdout = _SAMPLE_LOG_LINE + "\n" + _SAMPLE_LOG_LINE
        stderr = ""

    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: _Result())
    samples = cp.collect_tabbyapi_raw_samples(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    )
    assert len(samples) == 2
    assert samples[0] == {
        "tokens_generated": 12,
        "generation_seconds": 0.5,
        "queue_seconds": 0.0,
        "cached_tokens": 100,
        "new_tokens": 50,
        "process_speed_tps": 200.0,
    }


def test_collect_tabbyapi_raw_samples_empty_on_docker_failure(monkeypatch):
    def _raise(*a, **k):
        raise OSError("docker introuvable")

    monkeypatch.setattr(cp.subprocess, "run", _raise)
    samples = cp.collect_tabbyapi_raw_samples(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    )
    assert samples == []


def test_aggregate_prefill_stats_sums_and_counts_cache_zero():
    samples = [
        {"cached_tokens": 0, "new_tokens": 100, "process_speed_tps": 100.0},
        {"cached_tokens": 50, "new_tokens": 50, "process_speed_tps": 50.0},
    ]
    stats = cp.aggregate_prefill_stats(samples)
    assert stats == {
        "prefill_seconds": 2.0,
        "cache_zero_requests": 1,
        "tabbyapi_requests": 2,
        "prompt_tokens_total": 200,
    }


def test_aggregate_prefill_stats_empty_samples():
    assert cp.aggregate_prefill_stats([]) == {
        "prefill_seconds": 0.0,
        "cache_zero_requests": 0,
        "tabbyapi_requests": 0,
        "prompt_tokens_total": 0,
    }


# ─────────────────────────────────────────────────────────────────────────
# Identifiant de campagne + écriture/lecture JSON (jamais réécrit ensuite)
# ─────────────────────────────────────────────────────────────────────────


def test_campaign_id_slugifies_label_and_includes_timestamp():
    fixed_now = lambda: datetime(2026, 7, 26, 10, 30, 0, tzinfo=timezone.utc)  # noqa: E731
    cid = cp.campaign_id("Campagne A (budget par défaut)", now=fixed_now)
    assert cid == "20260726T103000Z-campagne-a-budget-par-d-faut"


def test_campaign_id_defaults_to_campagne_on_empty_slug():
    fixed_now = lambda: datetime(2026, 7, 26, 10, 30, 0, tzinfo=timezone.utc)  # noqa: E731
    assert cp.campaign_id("!!!", now=fixed_now).endswith("campagne")


def test_write_then_read_campaign_json_roundtrips(tmp_path):
    path = cp.campaign_json_path(tmp_path, "20260726T103000Z-test")
    metadata = {"commit": "abc123", "label": "test"}
    rows = [{"task_id": "T1", "repetition": 1, "success": True}]

    cp.write_campaign_json(path, metadata, "2026-07-26T10:00:00+00:00", "2026-07-26T10:05:00+00:00", rows)
    persisted = cp.read_campaign_json(path)

    assert persisted["metadata"]["commit"] == "abc123"
    assert persisted["metadata"]["started_at"] == "2026-07-26T10:00:00+00:00"
    assert persisted["metadata"]["ended_at"] == "2026-07-26T10:05:00+00:00"
    assert persisted["runs"] == rows


def test_campaign_json_path_uses_campaign_prefix(tmp_path):
    path = cp.campaign_json_path(tmp_path, "abc")
    assert path.name == "campaign-abc.json"


# ─────────────────────────────────────────────────────────────────────────
# Fichier de progression live (B2 Part 1.1, docs/briefs/B2-campaign-control.md)
# ─────────────────────────────────────────────────────────────────────────


def test_progress_json_path_uses_progress_suffix(tmp_path):
    path = cp.progress_json_path(tmp_path, "abc")
    assert path.name == "abc.progress.json"


def test_config_digest_stable_across_unrelated_field_changes():
    base = {"commit": "abc", "image_ids": {"a": "sha256:1"}, "env_flags": {"X": "1"}, "label": "l1"}
    other_label = {**base, "label": "l2"}
    assert cp.config_digest(base) == cp.config_digest(other_label)


def test_config_digest_changes_when_commit_drifts():
    base = {"commit": "abc", "image_ids": {}, "env_flags": {}}
    drifted = {"commit": "def", "image_ids": {}, "env_flags": {}}
    assert cp.config_digest(base) != cp.config_digest(drifted)


_PLANNED_T1_T1_T2 = [
    {"task_id": "T1", "repetition": 1},
    {"task_id": "T1", "repetition": 2},
    {"task_id": "T2", "repetition": 1},
]


def test_init_progress_state_total_runs_derived_from_planned():
    state = cp.init_progress_state("cid", "label", "2026-07-29T10:00:00Z", "digest123", _PLANNED_T1_T1_T2)
    assert state["total_runs"] == 3
    assert state["current"] is None
    assert state["completed"] == []
    assert state["paused"] is False


def test_init_progress_state_opens_segment_zero():
    state = cp.init_progress_state("cid", "label", "2026-07-29T10:00:00Z", "digest123", _PLANNED_T1_T1_T2)
    assert state["segments"] == [{"index": 0, "started_at": "2026-07-29T10:00:00Z", "ended_at": None}]


def test_write_then_read_progress_json_roundtrips(tmp_path):
    path = cp.progress_json_path(tmp_path, "cid")
    state = cp.init_progress_state("cid", "label", "2026-07-29T10:00:00Z", "digest123", _PLANNED_T1_T1_T2)
    cp.write_progress_json(path, state)
    persisted = cp.read_campaign_json(path)  # generic JSON reader, reused
    assert persisted == state


def test_write_progress_json_overwrites_atomically_no_leftover_tmp(tmp_path):
    path = cp.progress_json_path(tmp_path, "cid")
    state = cp.init_progress_state("cid", "label", "2026-07-29T10:00:00Z", "digest123", _PLANNED_T1_T1_T2)
    cp.write_progress_json(path, state)
    state["current"] = {"task_id": "T1", "repetition": 1, "thread_id": "x", "started_at": "now"}
    cp.write_progress_json(path, state)
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert cp.read_campaign_json(path)["current"]["task_id"] == "T1"


# ─────────────────────────────────────────────────────────────────────────
# Pause/reprise (B2 Part 2-3, docs/briefs/B2-campaign-control.md)
# ─────────────────────────────────────────────────────────────────────────


def test_pause_sentinel_path_uses_pause_suffix(tmp_path):
    path = cp.pause_sentinel_path(tmp_path, "abc")
    assert path.name == "abc.pause"


def test_open_new_segment_appends_and_returns_new_index():
    state = cp.init_progress_state("cid", "label", "t0", "digest", [])
    fixed_now = lambda: datetime(2026, 7, 30, 9, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    index = cp.open_new_segment(state, now=fixed_now)
    assert index == 1
    assert state["segments"][1] == {"index": 1, "started_at": "2026-07-30T09:00:00+00:00", "ended_at": None}


def test_close_current_segment_stamps_last_segment_only():
    state = cp.init_progress_state("cid", "label", "t0", "digest", [])
    cp.open_new_segment(state, now=lambda: datetime(2026, 7, 30, 9, 0, 0, tzinfo=timezone.utc))
    fixed_now = lambda: datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    cp.close_current_segment(state, now=fixed_now)
    assert state["segments"][0]["ended_at"] is None
    assert state["segments"][1]["ended_at"] == "2026-07-30T10:00:00+00:00"


def test_config_drift_diff_none_when_unchanged():
    metadata = {"commit": "abc", "image_ids": {"a": "sha256:1"}, "env_flags": {"X": "1"}}
    assert cp.config_drift_diff(metadata, dict(metadata)) is None


def test_config_drift_diff_reports_commit_image_and_flag_changes():
    recorded = {"commit": "abc", "image_ids": {"a": "sha256:1"}, "env_flags": {"X": "1"}}
    current = {"commit": "def", "image_ids": {"a": "sha256:2"}, "env_flags": {"X": "2"}}
    diff = cp.config_drift_diff(recorded, current)
    assert "commit: abc -> def" in diff
    assert "image[a]: sha256:1 -> sha256:2" in diff
    assert "flag[X]: 1 -> 2" in diff


def test_check_resume_staleness_none_when_within_threshold():
    state = {"segments": [{"index": 0, "started_at": "t0", "ended_at": "2026-07-29T10:00:00+00:00"}]}
    fixed_now = lambda: datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    assert cp.check_resume_staleness(state, max_days=7, now=fixed_now) is None


def test_check_resume_staleness_warns_past_threshold():
    state = {"segments": [{"index": 0, "started_at": "t0", "ended_at": "2026-07-01T10:00:00+00:00"}]}
    fixed_now = lambda: datetime(2026, 7, 30, 10, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    warning = cp.check_resume_staleness(state, max_days=7, now=fixed_now)
    assert warning is not None
    assert "29.0 jours" in warning


def test_check_resume_staleness_none_when_never_paused():
    state = {"segments": [{"index": 0, "started_at": "t0", "ended_at": None}]}
    assert cp.check_resume_staleness(state) is None


def test_append_campaign_row_accumulates_across_calls(tmp_path):
    path = cp.campaign_json_path(tmp_path, "cid")
    metadata = {"commit": "abc", "label": "l"}
    cp.append_campaign_row(path, metadata, "2026-07-29T10:00:00Z", {"task_id": "T1", "repetition": 1})
    cp.append_campaign_row(path, metadata, "2026-07-29T10:00:00Z", {"task_id": "T1", "repetition": 2})
    persisted = cp.read_campaign_json(path)
    assert [r["repetition"] for r in persisted["runs"]] == [1, 2]
    assert persisted["metadata"]["commit"] == "abc"


def test_write_campaign_json_atomic_no_leftover_tmp(tmp_path):
    path = cp.campaign_json_path(tmp_path, "cid")
    cp.write_campaign_json(path, {"commit": "abc"}, "t0", "t1", [{"task_id": "T1"}])
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert cp.read_campaign_json(path)["runs"] == [{"task_id": "T1"}]


# ─────────────────────────────────────────────────────────────────────────
# ETA (B2 Part 1.4) : somme par tâche restante, jamais une médiane globale
# ─────────────────────────────────────────────────────────────────────────


def test_normalize_duration_estimate_wraps_legacy_bare_float():
    assert cp.normalize_duration_estimate(42.0) == {"median": 42.0, "min": 42.0, "max": 42.0, "n": 1}


def test_normalize_duration_estimate_passes_through_dict():
    entry = {"median": 10.0, "min": 8.0, "max": 12.0, "n": 3}
    assert cp.normalize_duration_estimate(entry) is entry


def test_remaining_runs_matches_ordered_slice_when_completions_are_in_order():
    """Non-regression: the sequential case (still the default, N=1) must
    keep behaving exactly like the old planned[len(completed):] slice."""
    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T1", "repetition": 1}]}
    assert cp.remaining_runs(state) == [
        {"task_id": "T1", "repetition": 2},
        {"task_id": "T2", "repetition": 1},
    ]


def test_remaining_runs_correct_when_completions_are_out_of_order():
    """Effort 1.3 (docs/briefs/effort-1.3-parallel-campaigns.md): a second
    worker can finish a LATER planned entry before the first worker
    finishes an EARLIER one — planned[len(completed):] would then either
    replay an already-done run or skip a genuinely pending one. Here,
    T2 (the 3rd planned entry) completes before T1 rep 1 (the 1st)."""
    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T2", "repetition": 1}]}
    assert cp.remaining_runs(state) == [
        {"task_id": "T1", "repetition": 1},
        {"task_id": "T1", "repetition": 2},
    ]


def test_remaining_runs_empty_when_everything_completed_out_of_order():
    state = {
        "planned": _PLANNED_T1_T1_T2,
        "completed": [
            {"task_id": "T2", "repetition": 1},
            {"task_id": "T1", "repetition": 2},
            {"task_id": "T1", "repetition": 1},
        ],
    }
    assert cp.remaining_runs(state) == []


def test_compute_remaining_eta_sums_per_task_never_global_median():
    # Two tasks of very different duration: T_long (300s) x2 remaining,
    # T_short (10s) x1 remaining. A global-median approach (median of
    # {300,300,10} = 300, applied x3) would grossly overestimate — the
    # per-task sum must be 300+300+10, not 3x a shared median.
    state = {
        "planned": [
            {"task_id": "T_short", "repetition": 1},
            {"task_id": "T_long", "repetition": 1},
            {"task_id": "T_long", "repetition": 2},
        ],
        "completed": [],
    }
    estimates = {
        "T_short": {"median": 10.0, "min": 8.0, "max": 12.0, "n": 3},
        "T_long": {"median": 300.0, "min": 280.0, "max": 320.0, "n": 3},
    }
    eta = cp.compute_remaining_eta(state, estimates)
    assert eta["remaining_runs"] == 3
    assert eta["median_seconds"] == 610.0
    assert eta["min_seconds"] == 568.0
    assert eta["max_seconds"] == 652.0
    assert eta["reliable"] is True
    assert eta["unreliable_task_count"] == 0


def test_compute_remaining_eta_only_counts_runs_after_completed():
    state = {
        "planned": _PLANNED_T1_T1_T2,
        "completed": [{"task_id": "T1", "repetition": 1}],
    }
    estimates = {"T1": {"median": 5.0, "min": 5.0, "max": 5.0, "n": 1}, "T2": {"median": 7.0, "min": 7.0, "max": 7.0, "n": 1}}
    eta = cp.compute_remaining_eta(state, estimates)
    assert eta["remaining_runs"] == 2
    assert eta["median_seconds"] == 12.0


def test_compute_remaining_eta_correct_with_out_of_order_completions():
    """Same defect this was built to close as remaining_runs' own test
    above, exercised through the public ETA function a parallel campaign's
    dashboard actually calls."""
    state = {"planned": _PLANNED_T1_T1_T2, "completed": [{"task_id": "T2", "repetition": 1}]}
    estimates = {"T1": {"median": 5.0, "min": 5.0, "max": 5.0, "n": 1}, "T2": {"median": 7.0, "min": 7.0, "max": 7.0, "n": 1}}
    eta = cp.compute_remaining_eta(state, estimates)
    assert eta["remaining_runs"] == 2  # both T1 repetitions, not T2 (already done)
    assert eta["median_seconds"] == 10.0


def test_compute_remaining_eta_flags_cold_start_tasks_as_unreliable():
    state = {"planned": [{"task_id": "T_never_measured", "repetition": 1}], "completed": []}
    eta = cp.compute_remaining_eta(state, estimates={})
    assert eta["reliable"] is False
    assert eta["unreliable_task_count"] == 1
    assert eta["median_seconds"] == 0.0
