"""
Test ciblé de la compaction d'épisode (PLAN.md Phase 2, point 2) —
délibérément HORS du benchmark gelé (docs/benchmark-v1.md,
test_web_tasks.py) : une tâche conçue pour garantir >60 messages, jouée
flag OFF puis ON, jamais ajoutée à la suite officielle. Voir
docs/campaigns/2026-07-28_campaign_episode-compaction-enabled.md (requalifié
"non concluant" : 9-15% seulement de couverture sur les 11 tâches
officielles, trop courtes pour engager le mécanisme).

Tâche : parcourir les 30 fiches produit du catalogue local UNE PAR UNE
(lecture seule — évite la friction observée sur browser_fill_form lors
d'une première tentative avec un scénario de soumission de formulaire
répétée, voir docs/history.md si consigné) et calculer le prix total.
Choisie aussi pour tester directement le risque de perte d'information
(point 3c de la demande) : _summarize_subtask (app/graph.py) ne conserve
QUE la description de la sous-tâche + les arguments des tool_calls +
subtask["result"] (chaîne générique posée par verify_action, ex. "critère
atteint") — jamais le CONTENU retourné par les outils (ToolMessage). Si
une sous-tâche compactée portait le prix d'un produit, ce prix n'est
PAS dans le résumé structuré : la tâche ne peut réussir avec le flag ON
que si le total est calculé/consigné AVANT que la compaction n'efface la
sous-tâche correspondante, ou si le modèle recalcule/persiste
l'information autrement.

Réutilise les primitives de test_web_tasks.py (run_task, purges, comptage
d'audit) sans y toucher — importé, jamais modifié.

Nécessite MAX_TOOL_ITERATIONS relevé temporairement (>=45) : 30
consultations de fiche à ~1-2 tool_calls chacune dépassent le plafond par
défaut de 20 avant la fin de la tâche.

Usage :
    python3 probe_episode_compaction.py --flag-label off|on [--reps N]
"""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # pour "tests_integration" en package

from tests_integration import campaign_persistence  # noqa: E402
from tests_integration.test_web_tasks import (  # noqa: E402
    TABBYAPI_CONTAINER,
    _audit_entries,
    _derive_thread_id,
    _purge_downloads_volume,
    _reset_browser_session,
    _reset_ghostdesk_desktop,
    run_task,
)

CATALOG_URL = "http://fixture-catalog/catalog"
N_PRODUCTS = 30
# Vérité terrain : régénérée depuis le générateur RÉEL du fixture (SEED=42,
# voir fixtures/catalog/generate_catalog.py), jamais recalculée à la main
# (une première tentative de reproduction manuelle a désynchronisé la
# séquence rng en oubliant la consommation de _stock() — CLAUDE.md #8,
# vérifier contre le code installé, pas deviner). Confirmé identique au
# fixture-catalog réellement servi (product-1 = PX-1001/14.65€).
TOTAL_PRICE_EXPECTED = 2812.80
TOTAL_PRICE_TOLERANCE = 0.02  # arrondi cumulé sur 30 additions


def _build_prompt(marker: str) -> str:
    return (
        f"Sur {CATALOG_URL}/page-1.html, ouvre CHAQUE fiche produit du catalogue "
        f"UNE PAR UNE ({N_PRODUCTS} produits au total, répartis sur 3 pages) et note le "
        "prix affiché sur chaque fiche avant de passer à la suivante. Ne saute aucune "
        "fiche et ne regroupe pas les consultations : une navigation individuelle par "
        f"produit. Une fois les {N_PRODUCTS} fiches consultées, calcule la somme totale "
        "des prix relevés et rapporte le résultat au centime près.\n"
        f"(essai {marker})"
    )


def _assert_total_price(text: str) -> tuple:
    import re

    matches = re.findall(r"(\d{1,5}[.,]\d{2})", text)
    for m in matches:
        value = float(m.replace(",", "."))
        if abs(value - TOTAL_PRICE_EXPECTED) <= TOTAL_PRICE_TOLERANCE:
            return True, f"total {value} trouvé dans la réponse (attendu {TOTAL_PRICE_EXPECTED})"
    return False, f"total attendu {TOTAL_PRICE_EXPECTED} absent de la réponse (valeurs vues : {matches})"


def run_one(rep: int, flag_label: str) -> dict:
    marker = f"{flag_label}-{uuid.uuid4().hex[:8]}"
    prompt = _build_prompt(marker)
    _purge_downloads_volume()
    _reset_browser_session()
    _reset_ghostdesk_desktop()

    wall_start = datetime.now(timezone.utc)
    result = run_task(prompt)
    wall_end = datetime.now(timezone.utc)

    if result.error:
        success, detail = False, result.error
    else:
        success, detail = _assert_total_price(result.final_text)

    thread_id = _derive_thread_id(prompt)
    try:
        entries = _audit_entries(thread_id)
    except Exception:
        entries = []
    compaction_entries = [e for e in entries if e.get("kind") == "message" and e.get("role") == "episode_compaction"]
    messages_max = max((e.get("content") or {}).get("messages_count", 0) for e in compaction_entries) if compaction_entries else 0
    compactions_applied = sum(1 for e in compaction_entries if (e.get("content") or {}).get("compacted"))

    samples = campaign_persistence.collect_tabbyapi_raw_samples(wall_start, wall_end, container=TABBYAPI_CONTAINER)
    prefill_stats = campaign_persistence.aggregate_prefill_stats(samples)

    row = {
        "flag": flag_label,
        "rep": rep,
        "success": success,
        "detail": detail,
        "approvals": result.approvals,
        "tool_calls_observed": result.tool_calls_observed,
        "duration_seconds": round(result.duration_seconds, 1),
        "messages_max": messages_max,
        "compactions_applied": compactions_applied,
        "prefill_seconds": prefill_stats["prefill_seconds"],
        "prompt_tokens_total": prefill_stats["prompt_tokens_total"],
        "cache_zero_requests": prefill_stats["cache_zero_requests"],
        "tabbyapi_requests": prefill_stats["tabbyapi_requests"],
        "final_text": result.final_text,
    }
    print(
        f"[{flag_label}] rep {rep}: success={success} messages_max={messages_max} "
        f"compactions={compactions_applied} tool_calls={result.tool_calls_observed} "
        f"tokens_prompt={row['prompt_tokens_total']} duree={row['duration_seconds']}s",
        flush=True,
    )
    if not success:
        print(f"    -> {detail}", flush=True)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--flag-label", required=True, choices=["off", "on"])
    args = parser.parse_args()

    rows = []
    for rep in range(1, args.reps + 1):
        rows.append(run_one(rep, args.flag_label))

    out_path = Path(__file__).parent / f"probe_episode_compaction_{args.flag_label}.json"
    out_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"--- écrit {out_path} ---", flush=True)


if __name__ == "__main__":
    main()
