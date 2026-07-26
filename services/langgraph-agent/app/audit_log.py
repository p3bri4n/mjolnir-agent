"""
Journal d'audit (Phase 2, angle mort corrigé — voir HISTORY.md,
investigation T9) : trace machine-lisible de chaque tool_call effectivement
exécuté dont le tier n'est pas TIER_READ (silencieux par design, rien de
nouveau à auditer) — TIER_REVERSIBLE comme TIER_SENSITIVE, qu'il vienne
d'auto_call_tools (auto-approuvé) ou de call_tools (après approbation,
humaine ou via le harnais de campagne — voir _execute_tool_calls,
app/graph.py). Auparavant, seul auto_call_tools journalisait : un tour
passé par require_approval était supposé "déjà tracé dans l'historique de
conversation", supposition fausse en campagne automatisée (aucun humain ne
regarde) et de toute façon caduque après un redémarrage du service
(checkpointer MemorySaver, en mémoire uniquement) — le tout premier appel
de chaque outil par thread, le plus utile à l'investigation, restait
invisible.

Résultat d'outil (Phase 1d-révisée, voir HISTORY.md "l'observabilité
d'abord") : chaque entrée porte désormais aussi le résultat TEL QUE VU PAR
LE MODÈLE (déjà tronqué/hiérarchisé par _truncate_browser_result côté
appelant — jamais la version brute, ce serait dupliquer une donnée que le
modèle n'a jamais reçue). Sans ça, l'archive ne permettait de reconstruire
que la SÉQUENCE d'appels (tool + arguments), jamais ce que l'agent a
réellement perçu à chaque étape — ce qui a bloqué la vérification stricte
des hypothèses 0a/0b lors du diagnostic T5/T8 (voir HISTORY.md). C'est
aussi la fondation du futur endpoint "contexte de l'agent" du dashboard.

Un fichier JSONL par jour, sous AUDIT_LOG_DIR (défaut /workspace/.audit,
partagé avec les serveurs MCP filesystem/git/terminal via le même bind
mount, voir docker-compose.yml). Rotation/compression (voir
AUDIT_LOG_MAX_BYTES/_rotate_if_needed) : la persistance des résultats
gonfle significativement le volume par rapport à tool+arguments seuls,
d'où la nécessité de borner la taille d'un fichier journalier plutôt que de
le laisser croître sans fin.
"""

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AUDIT_LOG_DIR = os.environ.get("AUDIT_LOG_DIR", "/workspace/.audit")

# Au-delà de cette taille, le fichier journalier du jour est compressé et
# archivé (suffixe ".N.jsonl.gz", N croissant) avant qu'une nouvelle écriture
# ne reparte sur un fichier ".jsonl" frais pour le même jour — la journée
# n'est donc plus garantie tenir dans un seul fichier une fois ce seuil
# franchi, contrairement à avant l'ajout des résultats d'outil.
AUDIT_LOG_MAX_BYTES = int(os.environ.get("AUDIT_LOG_MAX_BYTES", str(20 * 1024 * 1024)))


def _log_path_for(when: datetime) -> Path:
    return Path(AUDIT_LOG_DIR) / f"{when.strftime('%Y-%m-%d')}.jsonl"


def _rotate_if_needed(path: Path) -> None:
    if not path.exists() or path.stat().st_size < AUDIT_LOG_MAX_BYTES:
        return
    n = 1
    while (path.parent / f"{path.stem}.{n}.jsonl.gz").exists():
        n += 1
    archive = path.parent / f"{path.stem}.{n}.jsonl.gz"
    with path.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def _append_entry(entry: dict) -> None:
    path = _log_path_for(datetime.now(timezone.utc))
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_tool_call(
    thread_id: str, tool_name: str, arguments: dict, tier: str, result: Optional[dict] = None
) -> None:
    _append_entry(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "tool": tool_name,
            "arguments": arguments,
            "tier": tier,
            "result": result,
        }
    )


def log_message(thread_id: str, role: str, content) -> None:
    """
    Observabilité (Phase 1d-révisée, voir HISTORY.md "correctif extraction"
    -> "OBSERVABILITÉ") : persiste le message ASSISTANT (raisonnement
    <think> inclus + réponse finale, voir call_llm/app/graph.py) produit à
    chaque tour — dernière pièce manquante de l'archive. Sans elle, une
    investigation d'archive ne pouvait reconstruire QUE ce que l'agent a
    perçu (résultats d'outils, voir log_tool_call) et sa séquence d'actions,
    jamais son propre raisonnement/texte — limite honnêtement signalée
    plusieurs fois pendant le diagnostic T1/T7/T10 (voir HISTORY.md).
    `kind: "message"` distingue ces entrées des tool_calls (`kind` absent
    pour ceux-ci, rétrocompatible) à la lecture — voir GET /audit,
    app/main.py, qui reste volontairement générique (renvoie tout, au
    consommateur de filtrer).
    """
    _append_entry(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
            "kind": "message",
            "role": role,
            "content": content,
        }
    )


def _iter_log_files(root: Path):
    """Fichiers journaliers, plein (.jsonl) puis archives compressées
    (.N.jsonl.gz) du même jour, triés par nom (donc par ordre chronologique
    de rotation) — voir _rotate_if_needed."""
    yield from sorted(root.glob("*.jsonl"))
    yield from sorted(root.glob("*.jsonl.gz"))


def read_entries(thread_id: Optional[str] = None) -> list:
    """
    Relit tous les fichiers journaliers (potentiellement plusieurs si la
    conversation a traversé un changement de jour ou une rotation par
    volume), triés par timestamp, optionnellement filtrés par thread_id.
    Usage : GET /audit (app/main.py). Une ligne corrompue individuelle est
    ignorée plutôt que de faire échouer toute la lecture — le journal reste
    consultable même si un écrivain a été interrompu en plein milieu d'une
    ligne.
    """
    root = Path(AUDIT_LOG_DIR)
    if not root.exists():
        return []

    entries = []
    for path in _iter_log_files(root):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if thread_id is None or entry.get("thread_id") == thread_id:
                    entries.append(entry)

    entries.sort(key=lambda e: e.get("timestamp", ""))
    return entries
