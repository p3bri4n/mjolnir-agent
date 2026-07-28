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
