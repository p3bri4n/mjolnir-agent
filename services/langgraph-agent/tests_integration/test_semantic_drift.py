"""
Test d'intégration contre le vrai modèle Ollama (aucun mock) : garde-fou de
non-régression contre la dérive observée en usage réel sur la tâche "va sur
google.fr" (voir README, tableau des bugs) — le modèle décrochait en cascade
de synonymes de plus en plus incohérents, ou en boucle de répétition de
phrases, sans jamais produire de tool_calls, jusqu'à saturer tout le contexte.
Cause racine (Modelfile agent-llm trop agressif en pénalités anti-répétition)
corrigée séparément ; ce test vérifie que ça reste vrai dans le temps, y
compris après un changement de modèle source (voir scripts/rebuild-agent-llm.sh).

Volontairement séparé de tests/ (suite unitaire, tout mocké via
tests/conftest.py, qui force LLM_BASE_URL vers un faux serveur à l'import) :
celui-ci parle aux vrais conteneurs Docker (langgraph-agent, mcp-client) via
`docker exec`, donc lent (dizaines de secondes, temps de génération LLM réel)
et non déterministe par nature. Ignoré par défaut ; opt-in explicite :

    RUN_LIVE_LLM_TESTS=1 python -m pytest tests_integration/ -v

Prérequis : `docker compose up` avec langgraph-agent/mcp-client/ollama actifs.
"""
import json
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM_TESTS") != "1",
    reason="test d'intégration live (LLM réel) : opt-in via RUN_LIVE_LLM_TESTS=1, nécessite docker compose up",
)

AGENT_CONTAINER = os.environ.get("LANGGRAPH_AGENT_CONTAINER", "langgraph-agent")

# Une réponse saine sur cette tâche est soit une pause d'approbation (le
# modèle a décidé d'appeler browser_navigate, etc. — quelques dizaines de
# mots), soit un court message texte. Une dérive (sémantique ou boucle de
# répétition) produit systématiquement des réponses bien plus longues, sans
# jamais atteindre de tool_calls. Seuil large exprès pour ne pas être fragile
# sur la formulation exacte du modèle, tout en attrapant les dérives déjà
# observées (~9800 tokens / plusieurs centaines de mots).
MAX_HEALTHY_WORD_COUNT = 150

# Détecte la boucle de répétition de phrases (l'autre variante de dérive
# documentée dans le README) : un même trigramme de mots revenant beaucoup
# plus souvent que ce qu'un texte cohérent produirait naturellement.
MAX_TRIGRAM_REPETITIONS = 5


def _docker_exec_python(container: str, script: str, timeout: int = 150) -> str:
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python3", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker exec dans {container} a échoué : {result.stderr}")
    return result.stdout


def _chat(content: str) -> str:
    payload = json.dumps(
        {"model": "agent-llm", "messages": [{"role": "user", "content": content}], "stream": False}
    )
    script = f"""
import json, urllib.request, urllib.error
req = urllib.request.Request(
    'http://localhost:8000/v1/chat/completions',
    data={payload!r}.encode(),
    headers={{'Content-Type': 'application/json'}},
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    print(json.dumps({{"ok": True, "content": data["choices"][0]["message"]["content"]}}))
except urllib.error.HTTPError as e:
    print(json.dumps({{"ok": False, "error": e.read().decode()}}))
"""
    raw = _docker_exec_python(AGENT_CONTAINER, script)
    result = json.loads(raw)
    if not result["ok"]:
        pytest.fail(f"Requête à langgraph-agent en échec : {result['error']}")
    return result["content"]


def _max_trigram_repetition(text: str) -> int:
    words = re.findall(r"\w+", text.lower())
    if len(words) < 3:
        return 0
    counts: dict[tuple, int] = {}
    for i in range(len(words) - 2):
        trigram = tuple(words[i : i + 3])
        counts[trigram] = counts.get(trigram, 0) + 1
    return max(counts.values(), default=0)


def test_va_sur_google_fr_does_not_drift():
    # Prompt unique par exécution pour éviter de retomber sur un thread_id
    # déjà résolu par un précédent tour (voir _derive_thread_id, app/main.py).
    content = _chat(f"[test intégration {os.getpid()}] va sur google.fr")

    word_count = len(content.split())
    assert word_count <= MAX_HEALTHY_WORD_COUNT, (
        f"Réponse de {word_count} mots (seuil {MAX_HEALTHY_WORD_COUNT}) — signe probable d'une "
        f"dérive du modèle plutôt qu'une décision d'outil normale. Contenu : {content[:500]}..."
    )

    repetitions = _max_trigram_repetition(content)
    assert repetitions <= MAX_TRIGRAM_REPETITIONS, (
        f"Trigramme de mots répété {repetitions} fois — signe probable d'une boucle de "
        f"répétition. Contenu : {content[:500]}..."
    )
