"""
Backfill BORNÉ (constat de l'inventaire de persistance, voir docs/history.md
"INVENTAIRE DE PERSISTANCE" puis "PERSISTANCE DES CAMPAGNES") : reconstruit
un index best-effort campagne -> fenêtre temporelle depuis les artefacts
DÉJÀ existants (rapports Markdown, fichiers .DONE), pour les campagnes
antérieures à ce chantier qui n'ont jamais eu de
campaign-<timestamp>-<label>.json. Ne ressuscite AUCUNE métrique perdue
(prefill/tokens/tool_calls par run restent hors de portée pour ces
campagnes, voir le constat) — rend seulement l'audit JSONL existant
(/workspace/.audit, jamais purgé) navigable rétroactivement : une fois la
fenêtre temporelle d'une campagne connue, GET /audit peut être filtré par
timestamp pour cette fenêtre (le filtrage par thread_id exact reste
impossible sans le prompt exact, perdu lui aussi — voir le constat).

Reconstruction :
  - fin de campagne = timestamp du fichier .DONE ("Campagne terminée : ...")
    si présent, sinon la date "Générée automatiquement le ..." du rapport ;
  - début de campagne = fin moins la SOMME des durées "durée=X.Xs" listées
    dans "## Détail par run" — approximatif par construction (ignore les
    pauses d'approbation manuelle, les purges/reset entre runs, etc.),
    signalé explicitement via "window_precision": "approximate".

Usage : python3 backfill_campaigns_index.py (depuis n'importe où — chemins
relatifs à ce fichier, pas au répertoire courant). Écrit
docs/campaigns/campaigns-index.json. Best-effort : un rapport
illisible/sans date est inclus avec window=null plutôt que de faire
échouer tout le backfill.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# Phase 2 (restructuration+anglais) : les rapports ont déménagé sous
# docs/campaigns/ (convention AAAA-MM-JJ_type_label.md/.DONE), l'index les
# accompagne au même endroit plutôt que de rester isolé dans
# tests_integration/, loin de ce qu'il indexe.
CAMPAIGNS_DIR = Path(__file__).parents[3] / "docs" / "campaigns"
OUTPUT_PATH = CAMPAIGNS_DIR / "campaigns-index.json"

_GENERATED_RE = re.compile(r"Générée automatiquement le ([0-9T:.+Z-]+)")
_DONE_END_RE = re.compile(r"Campagne terminée\s*:\s*([0-9T:Z-]+)")
_SCORE_RE = re.compile(r"\*\*(Score[^*]*)\*\*")
_DURATION_RE = re.compile(r"durée=([\d.]+)s")


def _parse_iso(value: str):
    """Best-effort : accepte les deux formats rencontrés dans ce dépôt
    (avec/sans microsecondes, 'Z' ou '+00:00'). None si non parsable —
    n'échoue jamais le backfill pour une date au format inattendu."""
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_report(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="ignore")
    generated_match = _GENERATED_RE.search(text)
    generated_at = generated_match.group(1) if generated_match else None

    score_match = _SCORE_RE.search(text)
    score = score_match.group(1) if score_match else None

    durations = [float(d) for d in _DURATION_RE.findall(text)]
    total_duration_seconds = round(sum(durations), 1) if durations else None

    done_path = path.with_suffix(".DONE")
    ended_at = None
    if done_path.exists():
        done_text = done_path.read_text(encoding="utf-8", errors="ignore")
        done_match = _DONE_END_RE.search(done_text)
        if done_match:
            ended_at = done_match.group(1)
    if ended_at is None:
        ended_at = generated_at

    ended_dt = _parse_iso(ended_at)
    started_at = None
    if ended_dt is not None and total_duration_seconds is not None:
        started_at = (ended_dt - timedelta(seconds=total_duration_seconds)).isoformat()

    return {
        "report": path.name,
        "has_done_marker": done_path.exists(),
        "started_at": started_at,
        "ended_at": ended_at,
        "total_duration_seconds": total_duration_seconds,
        "window_precision": "approximate" if started_at else "unknown",
        "window_precision_note": (
            "started_at = ended_at - somme des durées par run : ignore les pauses "
            "d'approbation manuelle et les purges/reset entre runs, donc antérieur "
            "au vrai début réel — jamais postérieur (best-effort, pas exact)"
        ),
        "score": score.strip() if score else None,
    }


def build_index() -> dict:
    reports = sorted(CAMPAIGNS_DIR.glob("*.md"))
    entries = {}
    for report_path in reports:
        try:
            entries[report_path.stem] = _parse_report(report_path)
        except OSError:
            entries[report_path.stem] = {"report": report_path.name, "error": "illisible"}
    return {
        "_note": (
            "Index BEST-EFFORT reconstruit après coup (backfill_campaigns_index.py) "
            "pour les campagnes antérieures à campaign_persistence.py — ne contient "
            "PAS les métriques perdues (voir docs/history.md, constat), seulement une "
            "fenêtre temporelle approximative pour naviguer /workspace/.audit "
            "rétroactivement. Les campagnes postérieures à ce chantier ont leur "
            "propre campaign-<timestamp>-<label>.json, précis, à préférer à cet index."
        ),
        "campaigns": entries,
    }


if __name__ == "__main__":
    index = build_index()
    OUTPUT_PATH.write_text(json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(index['campaigns'])} campagnes indexées -> {OUTPUT_PATH}")
