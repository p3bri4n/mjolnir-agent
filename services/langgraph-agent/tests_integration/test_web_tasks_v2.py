"""
Benchmark v2 harness (docs/briefs/B3-benchmark-v2.md, validated at
checkpoint 2026-07-30) — first slice: family F (regression core) only.
Families A-E (long horizon, policy compliance/CuP, hostile content,
honesty, perception channels) are not built yet.

Family F carries v1 tasks T3/T5/T6/T10 over VERBATIM — same wording, same
fixture, same assertion — imported directly from test_web_tasks.py rather
than copy-pasted, so "verbatim" is enforced by identity, not by eyeballing
two copies staying in sync (see test_family_f_matches_v1_verbatim,
tests/test_web_tasks_v2.py). 2 repetitions instead of 3 (brief: "an alarm
does not need the statistical power of a measurement").

Runner duplication accepted for now (docs/briefs/B3-benchmark-v2.md, no
explicit call on this): this module's _run_campaign_v2/_write_report_v2
are structurally the same shape as test_web_tasks.py's _run_campaign/
_write_report — campaign_persistence.py/campaign_preflight.py are
already generic and fully reused, but the run-loop control flow itself
lives twice for now. Extracting a shared "campaign engine" is deferred
until family B (the next, harder slice) makes the shared need concrete
rather than hypothetical (CLAUDE.md: no premature abstraction).

    RUN_LIVE_AGENT_TESTS=1 python -m pytest tests_integration/test_web_tasks_v2.py -v
"""
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests_integration import campaign_persistence, campaign_preflight
from tests_integration.test_web_tasks import (
    CAMPAIGNS_DIR,
    KNOWN_URLS_BY_TASK,
    TASKS as V1_TASKS,
    _classify_failure_cause,
    _derive_thread_id,
    _purge_downloads_volume,
    _reset_browser_session,
    _reset_ghostdesk_desktop,
    _update_duration_stats,
    run_task,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AGENT_TESTS") != "1",
    reason="live integration test (real web agent): opt-in via RUN_LIVE_AGENT_TESTS=1, "
    "requires docker compose up + test-fixtures profile",
)

# Family F task_ids (docs/briefs/B3-benchmark-v2.md) — order matches v1's
# TASKS list, so this filter naturally yields T3, T5, T6, T10 in that
# order without hard-coding a separate ordering to keep in sync.
FAMILY_F_TASK_IDS = [
    "T3_tableau_dynamique",
    "T5_telechargement_calcul",
    "T6_session_authentifiee",
    "T10_books_toscrape",
]
FAMILY_F_TASKS = [t for t in V1_TASKS if t[0] in FAMILY_F_TASK_IDS]
if len(FAMILY_F_TASKS) != len(FAMILY_F_TASK_IDS):
    raise RuntimeError(
        f"famille F attend {FAMILY_F_TASK_IDS!r} dans test_web_tasks.TASKS, "
        f"trouvé {[t[0] for t in FAMILY_F_TASKS]!r} — une tâche v1 a été renommée/supprimée"
    )

N_REPETITIONS_V2 = int(os.environ.get("WEB_TASKS_V2_REPETITIONS", "2"))
CAMPAIGN_LABEL_V2 = os.environ.get("WEB_TASKS_V2_CAMPAIGN_LABEL", "Benchmark v2 — famille F (régression)")
RESUME_CAMPAIGN_ID_V2 = os.environ.get("WEB_TASKS_V2_RESUME_CAMPAIGN_ID", "").strip() or None
CAMPAIGN_RESUME_STALENESS_DAYS_V2 = int(os.environ.get("CAMPAIGN_RESUME_STALENESS_DAYS", "7"))
CAMPAIGN_PAUSED_EXIT_CODE = 75  # same convention as test_web_tasks.py


def _report_path_v2() -> Path:
    default = CAMPAIGNS_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}_campaign-v2_famille-f.md"
    return Path(os.environ.get("WEB_TASKS_V2_REPORT_PATH", str(default)))


def _run_campaign_v2(resume_cid: str = None):
    """Same shape as test_web_tasks._run_campaign — see that function for
    the fuller commentary on pause/resume/segments (docs/briefs/archive/
    A6-campaign-control.md); trimmed here to what family F needs."""
    campaign_preflight.run_preflight(
        purge_downloads=_purge_downloads_volume,
        reset_browser_session=_reset_browser_session,
    )

    tasks_by_id = {t[0]: t for t in FAMILY_F_TASKS}
    metadata_now = campaign_persistence.collect_metadata(CAMPAIGN_LABEL_V2)
    digest_now = campaign_persistence.config_digest(metadata_now)

    if resume_cid:
        cid = resume_cid
        progress_path = campaign_persistence.progress_json_path(CAMPAIGNS_DIR, cid)
        if not progress_path.exists():
            raise RuntimeError(f"reprise impossible : {progress_path} introuvable")
        state = campaign_persistence.read_campaign_json(progress_path)
        if not state.get("paused"):
            raise RuntimeError(f"campagne {cid} n'est pas en pause (paused=false dans {progress_path})")

        json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
        campaign_data = campaign_persistence.read_campaign_json(json_path)
        rows = campaign_data["runs"]
        metadata = campaign_data["metadata"]
        started_at = metadata["started_at"]

        drift = campaign_persistence.config_drift_diff(metadata, metadata_now)
        if drift:
            raise campaign_preflight.PreflightError(
                f"reprise refusée : la configuration a dérivé depuis le lancement de {cid} ({drift})"
            )
        staleness_warning = campaign_persistence.check_resume_staleness(state, CAMPAIGN_RESUME_STALENESS_DAYS_V2)
        if staleness_warning:
            print(f"AVERTISSEMENT : {staleness_warning}")

        segment_index = campaign_persistence.open_new_segment(state)
        state["paused"] = False
        campaign_persistence.write_progress_json(progress_path, state)
    else:
        metadata = metadata_now
        cid = campaign_persistence.campaign_id(CAMPAIGN_LABEL_V2)
        started_at = datetime.now(timezone.utc).isoformat()
        progress_path = campaign_persistence.progress_json_path(CAMPAIGNS_DIR, cid)
        json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
        planned = [
            {"task_id": task_id, "repetition": rep}
            for task_id, _, _ in FAMILY_F_TASKS for rep in range(1, N_REPETITIONS_V2 + 1)
        ]
        state = campaign_persistence.init_progress_state(cid, CAMPAIGN_LABEL_V2, started_at, digest_now, planned)
        campaign_persistence.write_progress_json(progress_path, state)
        rows = []
        segment_index = 0

    pause_path = campaign_persistence.pause_sentinel_path(CAMPAIGNS_DIR, cid)
    remaining = state["planned"][len(state["completed"]):]

    for entry in remaining:
        task_id, rep = entry["task_id"], entry["repetition"]
        base_prompt, assert_fn = tasks_by_id[task_id][1], tasks_by_id[task_id][2]

        prompt = f"{base_prompt} (essai {uuid.uuid4().hex[:8]})"
        thread_id = _derive_thread_id(prompt)
        state["current"] = {
            "task_id": task_id,
            "repetition": rep,
            "thread_id": thread_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        campaign_persistence.write_progress_json(progress_path, state)

        _purge_downloads_volume()
        _reset_browser_session()
        _reset_ghostdesk_desktop()
        result = run_task(prompt)
        ok, detail = (False, result.error) if result.error else assert_fn(result.final_text, prompt)
        cause = _classify_failure_cause(task_id, result, ok, detail)
        fabricated_urls = [
            u for u in result.observed_navigate_urls
            if KNOWN_URLS_BY_TASK.get(task_id) and u not in KNOWN_URLS_BY_TASK[task_id]()
        ]
        row = {
            "task_id": task_id,
            "repetition": rep,
            "thread_id": result.thread_id,
            "success": ok,
            "detail": detail,
            "approvals": result.approvals,
            "tool_calls_observed": result.tool_calls_observed,
            "verification_opportunities": result.verification_opportunities,
            "verification_exploitable": result.verification_exploitable,
            "prefill_seconds": result.prefill_seconds,
            "cache_zero_requests": result.cache_zero_requests,
            "tabbyapi_requests": result.tabbyapi_requests,
            "prompt_tokens_total": result.prompt_tokens_total,
            "tabbyapi_raw_samples": result.tabbyapi_raw_samples,
            "fabricated_urls": fabricated_urls,
            "duration_seconds": round(result.duration_seconds, 1),
            "failure_cause": cause,
            "final_text": result.final_text,
            "episode_compaction_messages_max": result.episode_compaction_messages_max,
            "episode_compaction_applied_count": result.episode_compaction_applied_count,
            "segment": segment_index,
        }
        rows.append(row)
        campaign_persistence.append_campaign_row(json_path, metadata, started_at, row)

        state["completed"].append(
            {
                "task_id": task_id,
                "repetition": rep,
                "status": "success" if ok else "failure",
                "failure_cause": cause,
                "duration_s": row["duration_seconds"],
                "tool_calls": row["tool_calls_observed"],
                "thread_id": thread_id,
                "approvals": row["approvals"],
                "fabricated_urls_count": len(row["fabricated_urls"]),
                "segment": segment_index,
            }
        )
        state["current"] = None
        campaign_persistence.write_progress_json(progress_path, state)

        if pause_path.exists():
            pause_path.unlink(missing_ok=True)
            campaign_persistence.close_current_segment(state)
            state["paused"] = True
            campaign_persistence.write_progress_json(progress_path, state)
            _update_duration_stats(rows)  # shared ESTIMATE_CACHE_PATH.estimates keyed by task_id — no v1/v2 clash
            pytest.exit(
                f"Campagne v2 {cid} mise en pause après {len(state['completed'])}/{state['total_runs']} runs "
                f"(segment {segment_index})",
                returncode=CAMPAIGN_PAUSED_EXIT_CODE,
            )

    _update_duration_stats(rows)
    campaign_persistence.close_current_segment(state)
    campaign_persistence.write_progress_json(progress_path, state)
    return rows, cid, metadata, started_at


def _write_report_v2(rows: list, report_path) -> None:
    """Trimmed _write_report — family F is 4 alarm tasks, not a scored
    suite: a plain per-task line is enough, no CuP/coverage columns (those
    belong to families A-E once built)."""
    lines = [
        f"# {CAMPAIGN_LABEL_V2} — alarmes de régression (v1 → v2, docs/briefs/B3-benchmark-v2.md)",
        "",
        f"Générée automatiquement le {datetime.now(timezone.utc).isoformat()} "
        f"({N_REPETITIONS_V2} répétitions/tâche). Ces 4 tâches (T3/T5/T6/T10) sont reprises "
        "MOT POUR MOT de v1 — jamais comparées à un score v1, ce sont des alarmes sur de la "
        "plomberie coûteuse à reconstruire (téléchargement, session authentifiée, site externe "
        "stable), pas une mesure de progrès.",
        "",
    ]
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)

    total_ok = total_n = 0
    for task_id in FAMILY_F_TASK_IDS:
        task_rows = by_task.get(task_id, [])
        n_ok = sum(1 for r in task_rows if r["success"])
        n = len(task_rows)
        total_ok += n_ok
        total_n += n
        causes = [r["failure_cause"] for r in task_rows if r["failure_cause"]]
        causes_str = f" (échecs : {', '.join(causes)})" if causes else ""
        lines.append(f"- **{task_id}** : {n_ok}/{n}{causes_str}")

    lines.insert(3, f"**Alarmes : {total_ok}/{total_n} passages réussis.**")
    lines.append("")
    lines.append("## Détail par run")
    lines.append("")
    for r in rows:
        status = "✅" if r["success"] else "❌"
        segment_note = f", segment={r.get('segment', 0)}" if len({r.get("segment", 0) for r in rows}) > 1 else ""
        lines.append(
            f"- {status} `{r['task_id']}` #{r['repetition']} — {r['detail']} "
            f"(durée={r['duration_seconds']}s"
            f"{', cause=' + r['failure_cause'] if r['failure_cause'] else ''}{segment_note})"
        )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_web_tasks_v2_baseline():
    rows, cid, metadata, started_at = _run_campaign_v2(resume_cid=RESUME_CAMPAIGN_ID_V2)
    ended_at = datetime.now(timezone.utc).isoformat()

    json_path = campaign_persistence.campaign_json_path(CAMPAIGNS_DIR, cid)
    campaign_persistence.write_campaign_json(json_path, metadata, started_at, ended_at, rows)

    persisted_rows = campaign_persistence.read_campaign_json(json_path)["runs"]
    _write_report_v2(persisted_rows, _report_path_v2())

    assert rows, "aucune tâche exécutée"
