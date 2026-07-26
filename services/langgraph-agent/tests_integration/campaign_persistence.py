"""
Persistance de campagne (constat de l'inventaire de persistance avant ce
chantier, voir HISTORY.md "INVENTAIRE DE PERSISTANCE") : un fichier
`campaign-<timestamp>-<label>.json` par campagne, écrit UNE SEULE FOIS à la
fin (jamais réécrit ensuite), à côté du rapport Markdown existant
(`_write_report`, test_web_tasks.py, devient une VUE sur ce JSON plutôt
qu'une source de vérité propre — voir sa docstring). Corrige les 4 lacunes
du constat : (1) aucune ligne par run n'existait hors du Markdown prose,
(2) le thread_id — clé de jointure avec /workspace/.audit — n'était
consigné nulle part, (3) seuls des agrégats de métriques TabbyAPI
survivaient, (4) rien ne fixait la config effective (commit, digests
d'image, modèle, flags) au moment du run.

Correction factuelle avant implémentation (CLAUDE.md #8 — toute affirmation
sur le comportement d'une lib se vérifie contre le code installé) :
TabbyAPI (image `agentic-ai-playground-tabbyapi`, vérifié dans
/app/endpoints/*/router.py de l'image réellement construite) N'EXPOSE PAS
d'endpoint /metrics Prometheus, contrairement à llama-server (voir
services/dashboard/app/prometheus.py et le commentaire déjà présent dans
docker-compose.yml, service dashboard : "Pas d'équivalent /metrics/{slots}
pour TabbyAPI à ce jour"). Il n'y a donc rien à "relever avant/après" sur un
endpoint qui n'existe pas. La seule source réelle de performance par
requête reste le texte des logs du conteneur (regex sur "N tokens generated
in ... Process: X cached tokens and Y new tokens at Z T/s", reprise ici de
l'ancien `_fetch_tabbyapi_prefill_stats` de test_web_tasks.py, qui
n'agrégeait QUE la somme/moyenne — voir aggregate_prefill_stats plus bas
pour l'équivalent agrégé, désormais dérivé des échantillons plutôt que
recalculé séparément) : `collect_tabbyapi_raw_samples` ci-dessous persiste
un échantillon PAR REQUÊTE journalisée dans la fenêtre du run, ce qui rend
un delta a posteriori calculable — l'intention de la demande — sans
prétendre à un endpoint fictif.
"""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

AGENT_CONTAINER = "langgraph-agent"
TABBYAPI_CONTAINER = "tabbyapi"

# Union de tous les os.environ.get(...) trouvés dans services/langgraph-agent/
# app/*.py (voir grep ayant servi à établir cette liste) : les flags qui
# pilotent réellement le comportement de l'agent, à l'exclusion des URLs
# internes de service (LLM_BASE_URL, MCP_CLIENT_URL...) qui sont fixes par
# construction du réseau docker-compose et n'apportent rien à un diagnostic
# de campagne.
CAMPAIGN_ENV_FLAGS = [
    "MAX_TOOL_ITERATIONS",
    "LLM_MAX_TOKENS",
    "PLANNER_ENABLED",
    "PLANNER_MAX_TOKENS",
    "PLANNER_THINKING_ENABLED",
    "VERIFICATION_ENABLED",
    "SUBTASK_ATTEMPT_BUDGET",
    "REPLAN_BUDGET",
    "PLAN_VALIDATION_ENABLED",
    "PLAN_JUDGE_ENABLED",
    "ADAPTIVE_THINKING",
    "MAX_IMAGES_IN_CONTEXT",
    "IMAGE_FORMAT_PASSTHROUGH",
    "IMAGE_TOKEN_ESTIMATE",
    "AUTO_APPROVAL_STREAK_LIMIT",
    "AUTO_APPROVED_TOOLS",
    "APPROVAL_RULES_PATH",
    "BROWSER_TOOL_OUTPUT_MAX_CHARS",
    "AFFORDANCE_THRESHOLD",
    "FABRICATION_LIMIT",
    "BROWSER_NAVIGATE_GUARDRAIL",
    "MAX_EMPTY_ANSWER_RETRIES",
    "AUDIT_LOG_MAX_BYTES",
    "TZ",
]

# Conteneurs dont l'image effectivement tournante fait partie de la config
# d'un run (README, "Arborescence") : les 3 services applicatifs propres à
# ce dépôt + le backend d'inférence. playwright-mcp/ghostdesk/ocr-service
# sont des images officielles non reconstruites par ce dépôt (voir README) :
# leur ID d'image est quand même capturé (utile pour détecter un `:latest`
# qui a bougé), juste jamais "construit localement" au sens preflight.
CAMPAIGN_IMAGE_CONTAINERS = [
    "langgraph-agent",
    "mcp-client",
    "tabbyapi",
    "playwright-mcp",
]


def _run(args: list, timeout: int = 15) -> Optional[str]:
    """Best-effort : une métadonnée de contexte manquante ne doit jamais faire
    échouer une campagne entière (voir _fetch_tabbyapi_prefill_stats,
    test_web_tasks.py, même philosophie)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_commit(repo_dir: Optional[Path] = None) -> Optional[str]:
    args = ["git", "rev-parse", "HEAD"]
    if repo_dir is not None:
        args = ["git", "-C", str(repo_dir), "rev-parse", "HEAD"]
    return _run(args)


def docker_image_id(container: str) -> Optional[str]:
    """ID de l'image RÉELLEMENT utilisée par le conteneur qui tourne (même
    primitive que campaign_preflight._fetch_tabbyapi_image_ids) — None si le
    conteneur n'existe pas/ne tourne pas, jamais une exception."""
    return _run(["docker", "inspect", "--format", "{{.Image}}", container])


def collect_image_digests(containers: list = None) -> dict:
    containers = containers if containers is not None else CAMPAIGN_IMAGE_CONTAINERS
    return {name: docker_image_id(name) for name in containers}


def fetch_tabbyapi_model_id(container: str = TABBYAPI_CONTAINER) -> Optional[str]:
    """`id` du ModelCard renvoyé par GET /v1/model (voir
    /app/endpoints/core/router.py de l'image tabbyapi, "Currently loaded
    model endpoint") — vérité terrain de ce qui est RÉELLEMENT chargé,
    plutôt que de relire config.yml (qui ne garantit pas que le rechargement
    a eu lieu). disable_auth: true (config.yml) : pas de clé requise."""
    script = """
import json, urllib.request
with urllib.request.urlopen('http://localhost:5000/v1/model', timeout=10) as r:
    print(json.loads(r.read().decode()).get('id'))
"""
    out = _run(["docker", "exec", "-i", container, "python3", "-c", script], timeout=15)
    return out or None


def collect_env_flags(container: str = AGENT_CONTAINER, flags: list = None) -> dict:
    """Flags D'ENV TELS QUE VUS PAR LE CONTENEUR qui tourne (pas le process
    hôte qui lance pytest, qui peut ne pas les avoir/en avoir des périmés) —
    `docker exec env`, filtré à CAMPAIGN_ENV_FLAGS. Absent du conteneur =
    absent du dict (pas de valeur par défaut inventée ici : app/*.py a déjà
    ses propres défauts, les dupliquer ici périmerait silencieusement à leur
    moindre changement)."""
    flags = flags if flags is not None else CAMPAIGN_ENV_FLAGS
    out = _run(["docker", "exec", container, "env"])
    if out is None:
        return {}
    found = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in flags:
            found[key] = value
    return found


def collect_metadata(label: str, repo_dir: Optional[Path] = None) -> dict:
    return {
        "commit": git_commit(repo_dir),
        "image_ids": collect_image_digests(),
        "tabbyapi_model_id": fetch_tabbyapi_model_id(),
        "env_flags": collect_env_flags(),
        "label": label,
    }


_TABBY_METRICS_RE = re.compile(
    r"(\d+) tokens generated in ([\d.]+) seconds \(Queue: ([\d.]+) s, Process: (\d+) cached tokens "
    r"and (\d+) new tokens at ([\d.]+) T/s"
)


def collect_tabbyapi_raw_samples(since_dt: datetime, until_dt: datetime, container: str = TABBYAPI_CONTAINER) -> list:
    """Un échantillon PAR REQUÊTE TabbyAPI journalisée dans la fenêtre
    [since_dt, until_dt] (voir docstring du module pour pourquoi ce n'est
    pas un relevé /metrics). Best-effort, jamais d'exception : renvoie []
    si `docker logs` échoue (conteneur redémarré/arrêté entre-temps)."""
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_iso = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", since_iso, "--until", until_iso, container],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = (result.stdout or "") + (result.stderr or "")
    normalized = re.sub(r"\s+", " ", text)
    samples = []
    for m in _TABBY_METRICS_RE.finditer(normalized):
        samples.append(
            {
                "tokens_generated": int(m.group(1)),
                "generation_seconds": float(m.group(2)),
                "queue_seconds": float(m.group(3)),
                "cached_tokens": int(m.group(4)),
                "new_tokens": int(m.group(5)),
                "process_speed_tps": float(m.group(6)),
            }
        )
    return samples


def aggregate_prefill_stats(samples: list) -> dict:
    """Même calcul que l'ancien _fetch_tabbyapi_prefill_stats
    (test_web_tasks.py, avant ce chantier) mais depuis les échantillons déjà
    collectés par collect_tabbyapi_raw_samples — évite un second `docker
    logs` sur la même fenêtre temporelle pour le même résultat."""
    prefill_seconds = 0.0
    cache_zero = 0
    for s in samples:
        if s["cached_tokens"] == 0:
            cache_zero += 1
        if s["process_speed_tps"] > 0:
            prefill_seconds += s["new_tokens"] / s["process_speed_tps"]
    return {
        "prefill_seconds": round(prefill_seconds, 2),
        "cache_zero_requests": cache_zero,
        "tabbyapi_requests": len(samples),
    }


def campaign_id(label: str, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    timestamp = now().strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "campagne"
    return f"{timestamp}-{slug}"


def campaign_json_path(directory: Path, cid: str) -> Path:
    return directory / f"campaign-{cid}.json"


def write_campaign_json(path: Path, metadata: dict, started_at: str, ended_at: str, rows: list) -> None:
    """Écrit le fichier UNE SEULE FOIS (voir docstring du module) : appelé
    une fois par campagne, à la toute fin, jamais en cours de route ni
    réécrit ensuite — cohérent avec `campaign-<timestamp>-<label>.json`
    demandé, pas un fichier vivant mis à jour au fil des runs."""
    payload = {
        "metadata": {**metadata, "started_at": started_at, "ended_at": ended_at},
        "runs": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def read_campaign_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
