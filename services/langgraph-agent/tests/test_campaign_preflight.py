"""
Préambule de campagne (Itération 0, docs/briefs/phase-1-coeur-cognitif.md) :
tests_integration/campaign_preflight.py. Ces tests couvrent uniquement la
logique PURE (check_tools_schema) et l'orchestration de run_preflight avec
des callables injectés — jamais de docker exec réel ici, contrairement à
test_web_tasks.py (opt-in RUN_LIVE_AGENT_TESTS=1). Vit dans tests/ (suite
rapide, toujours exécutée) précisément parce que cette logique n'a pas
besoin de la stack live pour être vérifiée.
"""

import pytest

from tests_integration import campaign_preflight as preflight

# A device reading that satisfies check_device_placement against
# EXPECTED_GPU_DEVICES (identity + memory within tolerance) — distinct
# from EXPECTED_GPU_DEVICES itself, which is the expected SPEC (no
# memory_used_mib key), not a shape of what nvidia-smi actually reports.
_OK_GPU_DEVICES = [
    {"index": 0, "name": "NVIDIA GeForce RTX 5060 Ti", "bus_id": "00000000:04:00.0", "memory_used_mib": 6052.0},
    {
        "index": 1,
        "name": "NVIDIA GeForce RTX 4070 Ti SUPER",
        "bus_id": "00000000:08:00.0",
        "memory_used_mib": 12616.0,
    },
]


def test_check_tools_schema_ok_when_synced_and_complete():
    tools = preflight.EXPECTED_TOOLS | {"browser_extract", "browser_hover"}
    assert preflight.check_tools_schema(tools, tools) is None


def test_check_tools_schema_flags_desync_between_agent_and_mcp_client():
    # browser_hover : outil réel du serveur Playwright officiel, absent
    # d'EXPECTED_TOOLS (browser_snapshot y est entré le 2026-07-31,
    # passé TIER_READ — voir docs/resolved-bugs.md — ce qui rendait cet
    # exemple caduc : union avec un élément déjà présent = no-op, plus de
    # désynchronisation à détecter).
    agent_tools = preflight.EXPECTED_TOOLS
    mcp_tools = preflight.EXPECTED_TOOLS | {"browser_hover"}
    error = preflight.check_tools_schema(agent_tools, mcp_tools)
    assert error is not None
    assert "désynchronisé" in error
    assert "browser_hover" in error
    assert "docker compose restart langgraph-agent" in error


def test_check_tools_schema_flags_missing_expected_tool():
    incomplete = preflight.EXPECTED_TOOLS - {"browser_navigate"}
    error = preflight.check_tools_schema(incomplete, incomplete)
    assert error is not None
    assert "browser_navigate" in error


def test_run_preflight_raises_before_any_reset_on_desync():
    calls = []

    with pytest.raises(preflight.PreflightError):
        preflight.run_preflight(
            purge_downloads=lambda: calls.append("purge"),
            reset_browser_session=lambda: calls.append("reset"),
            fetch_agent_tools=lambda: preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: preflight.EXPECTED_TOOLS | {"nouvel_outil"},
            fetch_llm_ready=lambda: True,
            fetch_tabbyapi_image_ids=lambda: ("sha256:same", "sha256:same"),
            fetch_device_placement=lambda: _OK_GPU_DEVICES,
            fetch_agent_env=lambda: dict(preflight.EXPECTED_AGENT_FLAGS),
        )
    assert calls == [], "purge/reset ne doivent jamais tourner si le préambule échoue"


def test_run_preflight_purges_and_resets_when_schema_ok():
    calls = []

    preflight.run_preflight(
        purge_downloads=lambda: calls.append("purge"),
        reset_browser_session=lambda: calls.append("reset"),
        fetch_agent_tools=lambda: preflight.EXPECTED_TOOLS,
        fetch_mcp_tools=lambda: preflight.EXPECTED_TOOLS,
        fetch_llm_ready=lambda: True,
        fetch_tabbyapi_image_ids=lambda: ("sha256:same", "sha256:same"),
        fetch_device_placement=lambda: _OK_GPU_DEVICES,
        fetch_agent_env=lambda: dict(preflight.EXPECTED_AGENT_FLAGS),
        fetch_fixtures_reachable=lambda: {name: True for name in preflight.FIXTURE_URLS},
    )
    assert calls == ["purge", "reset"]


class _StopEarly(Exception):
    """Sentinelle : prouve juste que fetch_llm_ready est appelé AVANT le
    schéma, sans jamais attendre le vrai timeout (180s) de wait_for_llm_ready
    pour un fetch_llm_ready qui resterait False indéfiniment."""


def test_run_preflight_checks_llm_ready_before_schema():
    schema_calls = []

    def fetch_llm_ready():
        raise _StopEarly()

    with pytest.raises(_StopEarly):
        preflight.run_preflight(
            purge_downloads=lambda: None,
            reset_browser_session=lambda: None,
            fetch_agent_tools=lambda: schema_calls.append("agent") or preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: schema_calls.append("mcp") or preflight.EXPECTED_TOOLS,
            fetch_llm_ready=fetch_llm_ready,
        )
    assert schema_calls == [], "le schéma ne doit pas être comparé si le LLM ne répond pas"


# ─────────────────────────────────────────────────────────────────────────
# check_device_placement (docs/briefs/deterministic-gpu-placement.md, step 5)
# ─────────────────────────────────────────────────────────────────────────


def test_check_device_placement_ok_when_within_tolerance():
    assert preflight.check_device_placement(_OK_GPU_DEVICES) is None


def test_check_device_placement_flags_missing_device():
    devices = [preflight.EXPECTED_GPU_DEVICES[0] | {"memory_used_mib": 5 * 1024}]
    error = preflight.check_device_placement(devices)
    assert error is not None
    assert "carte absente" in error
    assert "RTX 4070 Ti SUPER" in error


def test_check_device_placement_flags_wrong_identity():
    devices = [
        {"index": 0, "name": "NVIDIA GeForce RTX 5060 Ti", "bus_id": "00000000:04:00.0", "memory_used_mib": 5 * 1024},
        {"index": 1, "name": "NVIDIA GeForce RTX 4070 Ti SUPER", "bus_id": "00000000:04:00.0", "memory_used_mib": 14 * 1024},
    ]
    error = preflight.check_device_placement(devices)
    assert error is not None
    assert "index 1" in error
    assert "00000000:08:00.0" in error


def test_check_device_placement_flags_reverted_to_autosplit():
    """The exact regression this check exists for: gpu_split_auto flipped
    back to true reproduces the ORIGINAL finding this whole brief fixes
    (14 GB on the 5060 Ti, 4.4 GB on the 4070 Ti SUPER) — both devices land
    far outside their configured tolerance band."""
    devices = [
        {"index": 0, "name": "NVIDIA GeForce RTX 5060 Ti", "bus_id": "00000000:04:00.0", "memory_used_mib": 14131.0},
        {
            "index": 1,
            "name": "NVIDIA GeForce RTX 4070 Ti SUPER",
            "bus_id": "00000000:08:00.0",
            "memory_used_mib": 4424.0,
        },
    ]
    error = preflight.check_device_placement(devices)
    assert error is not None
    assert "index 0" in error
    assert "index 1" in error
    assert "services/tabbyapi/config.yml" in error


def test_run_preflight_checks_device_placement_before_flags():
    flags_calls = []

    with pytest.raises(preflight.PreflightError, match="carte absente"):
        preflight.run_preflight(
            purge_downloads=lambda: None,
            reset_browser_session=lambda: None,
            fetch_agent_tools=lambda: preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: preflight.EXPECTED_TOOLS,
            fetch_llm_ready=lambda: True,
            fetch_tabbyapi_image_ids=lambda: ("sha256:same", "sha256:same"),
            fetch_device_placement=lambda: [],
            fetch_agent_env=lambda: flags_calls.append("flags") or dict(preflight.EXPECTED_AGENT_FLAGS),
        )
    assert flags_calls == [], "les flags ne doivent pas être comparés si le placement GPU a dérivé"


# ─────────────────────────────────────────────────────────────────────────
# check_agent_flags (docs/briefs/flags-du-coeur-cognitif.md, point 2)
# ─────────────────────────────────────────────────────────────────────────


def test_check_agent_flags_ok_when_matching_expected():
    assert preflight.check_agent_flags(dict(preflight.EXPECTED_AGENT_FLAGS)) is None


def test_check_agent_flags_flags_stale_override():
    actual = dict(preflight.EXPECTED_AGENT_FLAGS)
    actual["PLANNER_ENABLED"] = "false"
    error = preflight.check_agent_flags(actual)
    assert error is not None
    assert "PLANNER_ENABLED" in error
    assert "attendu='true'" in error
    assert "effectif='false'" in error
    assert "docker compose up -d --force-recreate langgraph-agent" in error


def test_check_agent_flags_treats_missing_key_as_empty_string():
    actual = dict(preflight.EXPECTED_AGENT_FLAGS)
    del actual["APPROVAL_RULES_PATH"]
    assert preflight.check_agent_flags(actual) is None, "APPROVAL_RULES_PATH attendu est déjà '' "


# ─────────────────────────────────────────────────────────────────────────
# CAMPAIGN_EXPECTED_FLAGS_OVERRIDE (effort 2's ablation campaigns) — the
# _fetch_agent_env fix (effort 2 point 3): a key introduced PURELY via the
# override, not already present in the base EXPECTED_AGENT_FLAGS, must
# still be fetched from the container. Every prior override use only
# flipped an existing key's value, so this path was untested until
# PLANNING_MODE (a genuinely new key) needed it.
# ─────────────────────────────────────────────────────────────────────────


def test_expected_agent_flags_merges_override(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_EXPECTED_FLAGS_OVERRIDE", '{"PLANNING_MODE": "merged", "PLANNER_ENABLED": "false"}')
    merged = preflight._expected_agent_flags()
    assert merged["PLANNING_MODE"] == "merged"
    assert merged["PLANNER_ENABLED"] == "false"
    # every other base key untouched
    assert merged["VERIFICATION_ENABLED"] == preflight.EXPECTED_AGENT_FLAGS["VERIFICATION_ENABLED"]


def test_fetch_agent_env_queries_override_only_keys(monkeypatch):
    """Regression for the gap fixed in effort 2 point 3: before the fix,
    _fetch_agent_env() queried list(EXPECTED_AGENT_FLAGS) (the base dict
    only) — an override-introduced key not already in that base dict
    would never be requested from the container, so check_agent_flags
    would always compare it against "" regardless of the real value."""
    monkeypatch.setenv("CAMPAIGN_EXPECTED_FLAGS_OVERRIDE", '{"PLANNING_MODE": "merged"}')
    captured = {}

    def fake_collect_env_flags(container, flags):
        captured["flags"] = flags
        return {}

    monkeypatch.setattr(preflight.campaign_persistence, "collect_env_flags", fake_collect_env_flags)
    preflight._fetch_agent_env()
    assert "PLANNING_MODE" in captured["flags"]


def test_run_preflight_checks_flags_before_schema_but_after_image_freshness():
    schema_calls = []

    with pytest.raises(preflight.PreflightError, match="PLANNER_ENABLED"):
        preflight.run_preflight(
            purge_downloads=lambda: None,
            reset_browser_session=lambda: None,
            fetch_agent_tools=lambda: schema_calls.append("agent") or preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: schema_calls.append("mcp") or preflight.EXPECTED_TOOLS,
            fetch_llm_ready=lambda: True,
            fetch_tabbyapi_image_ids=lambda: ("sha256:same", "sha256:same"),
            fetch_device_placement=lambda: _OK_GPU_DEVICES,
            fetch_agent_env=lambda: {**preflight.EXPECTED_AGENT_FLAGS, "PLANNER_ENABLED": "false"},
        )
    assert schema_calls == [], "le schéma ne doit pas être comparé si les flags sont désynchronisés"


# ─────────────────────────────────────────────────────────────────────────
# check_tabbyapi_image_fresh (arbitrage post-1/2-ter, voir docs/history.md)
# ─────────────────────────────────────────────────────────────────────────


def test_check_tabbyapi_image_fresh_ok_when_ids_match():
    assert preflight.check_tabbyapi_image_fresh(lambda: ("sha256:abc", "sha256:abc")) is None


def test_check_tabbyapi_image_fresh_flags_stale_container():
    error = preflight.check_tabbyapi_image_fresh(lambda: ("sha256:old", "sha256:new"))
    assert error is not None
    assert "sha256:old" in error
    assert "sha256:new" in error
    assert "docker compose up -d --build tabbyapi" in error


def test_run_preflight_checks_image_freshness_before_schema():
    schema_calls = []

    with pytest.raises(preflight.PreflightError, match="image différente"):
        preflight.run_preflight(
            purge_downloads=lambda: None,
            reset_browser_session=lambda: None,
            fetch_agent_tools=lambda: schema_calls.append("agent") or preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: schema_calls.append("mcp") or preflight.EXPECTED_TOOLS,
            fetch_llm_ready=lambda: True,
            fetch_tabbyapi_image_ids=lambda: ("sha256:old", "sha256:new"),
        )
    assert schema_calls == [], "le schéma ne doit pas être comparé si l'image tabbyapi est périmée"


# ─────────────────────────────────────────────────────────────────────────
# wait_for_llm_ready (horloge/sleep injectés, jamais de vrai délai)
# ─────────────────────────────────────────────────────────────────────────


def test_wait_for_llm_ready_returns_immediately_when_already_ready():
    sleeps = []
    preflight.wait_for_llm_ready(lambda: True, sleep=lambda s: sleeps.append(s), now=lambda: 0.0)
    assert sleeps == []


def test_wait_for_llm_ready_retries_until_success():
    attempts = [False, False, True]
    sleeps = []

    def fetch():
        return attempts.pop(0)

    preflight.wait_for_llm_ready(
        fetch, timeout_seconds=100, interval_seconds=5, sleep=lambda s: sleeps.append(s), now=lambda: 0.0
    )
    assert sleeps == [5, 5]


def test_wait_for_llm_ready_raises_after_timeout():
    clock = iter([0.0, 1.0, 2.0, 200.0])

    with pytest.raises(preflight.PreflightError, match="ne répond pas"):
        preflight.wait_for_llm_ready(
            lambda: False, timeout_seconds=100, interval_seconds=5, sleep=lambda s: None, now=lambda: next(clock)
        )


# ─────────────────────────────────────────────────────────────────────────
# check_fixtures_reachable (docs/campaigns/2026-07-28_campaign_post-rename-
# mjolnir.md — invalid 14/33 run, test-fixtures profile never started)
# ─────────────────────────────────────────────────────────────────────────


def test_check_fixtures_reachable_ok_when_all_reachable():
    reachability = {name: True for name in preflight.FIXTURE_URLS}
    assert preflight.check_fixtures_reachable(reachability) is None


def test_check_fixtures_reachable_flags_unreachable_fixture():
    reachability = {name: True for name in preflight.FIXTURE_URLS}
    reachability["fixture-catalog"] = False
    error = preflight.check_fixtures_reachable(reachability)
    assert error is not None
    assert "fixture-catalog" in error
    assert "docker compose --profile test-fixtures up -d" in error


def test_check_fixtures_reachable_treats_missing_key_as_unreachable():
    error = preflight.check_fixtures_reachable({})
    assert error is not None
    assert all(name in error for name in preflight.FIXTURE_URLS)


def test_run_preflight_checks_fixtures_after_schema_before_purge():
    calls = []

    with pytest.raises(preflight.PreflightError, match="injoignables"):
        preflight.run_preflight(
            purge_downloads=lambda: calls.append("purge"),
            reset_browser_session=lambda: calls.append("reset"),
            fetch_agent_tools=lambda: preflight.EXPECTED_TOOLS,
            fetch_mcp_tools=lambda: preflight.EXPECTED_TOOLS,
            fetch_llm_ready=lambda: True,
            fetch_tabbyapi_image_ids=lambda: ("sha256:same", "sha256:same"),
            fetch_device_placement=lambda: _OK_GPU_DEVICES,
            fetch_agent_env=lambda: dict(preflight.EXPECTED_AGENT_FLAGS),
            fetch_fixtures_reachable=lambda: {},
        )
    assert calls == [], "purge/reset ne doivent jamais tourner si les fixtures sont injoignables"
