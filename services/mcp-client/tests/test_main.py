"""
Tests de mcp-client : le registre de SERVERS est remplacé par un vrai petit
serveur MCP de test (process Python, transport stdio), pour vérifier la
logique réelle (registre d'outils, appel, gestion d'erreur) sans dépendre
du socket Docker ni des images mcp/* réelles.
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import StdioServerParameters

TEST_SERVER_PATH = Path(__file__).parent / "fixtures" / "echo_server.py"
TEST_HTTP_SERVER_PATH = Path(__file__).parent / "fixtures" / "echo_http_server.py"


@pytest.fixture(autouse=True)
def override_servers(monkeypatch):
    import app.main as main_mod

    main_mod.SERVERS = {
        "echo": {
            "transport": "stdio",
            "params": StdioServerParameters(command=sys.executable, args=[str(TEST_SERVER_PATH)]),
        },
    }
    main_mod._tool_registry.clear()
    yield
    main_mod._tool_registry.clear()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"le serveur de test n'a pas démarré sur le port {port}")


@pytest.fixture
def echo_http_server():
    """Lance le serveur MCP de test en Streamable HTTP, exige le token 'secret-token'."""
    port = _free_port()
    token = "secret-token"
    proc = subprocess.Popen([sys.executable, str(TEST_HTTP_SERVER_PATH), str(port), token])
    try:
        _wait_for_port(port)
        yield {"url": f"http://127.0.0.1:{port}/mcp", "token": token}
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def echo_http_server_with_model_space():
    """Comme echo_http_server, mais exige en plus GhostDesk-Model-Space: '1000'."""
    port = _free_port()
    token = "secret-token"
    model_space = "1000"
    proc = subprocess.Popen(
        [sys.executable, str(TEST_HTTP_SERVER_PATH), str(port), token, model_space]
    )
    try:
        _wait_for_port(port)
        yield {"url": f"http://127.0.0.1:{port}/mcp", "token": token, "model_space": model_space}
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture
def echo_http_server_rejecting_model_space_header():
    """Comme echo_http_server, mais échoue si un header GhostDesk-Model-Space est reçu."""
    port = _free_port()
    token = "secret-token"
    proc = subprocess.Popen(
        [sys.executable, str(TEST_HTTP_SERVER_PATH), str(port), token, ""]
    )
    try:
        _wait_for_port(port)
        yield {"url": f"http://127.0.0.1:{port}/mcp", "token": token}
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def _client():
    import app.main as main_mod
    return TestClient(main_mod.app)


def test_health():
    resp = _client().get("/health")
    assert resp.status_code == 200


def test_list_tools_builds_registry():
    resp = _client().get("/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == {"echo": "echo"}


def test_tools_schema_exposes_description_and_input_schema():
    """
    langgraph-agent consomme ce schéma pour lier les outils au LLM via
    bind_tools (voir services/langgraph-agent/app/graph.py). Sans
    description/inputSchema, le LLM ne peut pas savoir qu'un outil existe ni
    quels arguments il attend.
    """
    resp = _client().get("/tools/schema")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Renvoie le message reçu, préfixé de 'echo: '.",
                "parameters": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            },
        }
    ]


def test_call_known_tool_returns_result():
    resp = _client().post("/call", json={"tool": "echo", "arguments": {"message": "bonjour"}})
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert content[0]["text"] == "echo: bonjour"


def test_call_unknown_tool_returns_404():
    resp = _client().post("/call", json={"tool": "inconnu", "arguments": {}})
    assert resp.status_code == 404
    assert "inconnu" in resp.json()["detail"]


def test_http_server_list_and_call_with_valid_token(echo_http_server):
    import app.main as main_mod

    main_mod.SERVERS["desktop"] = {
        "transport": "http",
        "url": echo_http_server["url"],
        "token": echo_http_server["token"],
    }

    resp = _client().get("/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"]["echo"] == "desktop"

    resp = _client().post("/call", json={"tool": "echo", "arguments": {"message": "bonjour"}})
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "echo: bonjour"


def test_http_server_sends_model_space_header(echo_http_server_with_model_space):
    """
    Nécessaire aux modèles Qwen (voir GHOSTDESK_MODEL_SPACE dans app/main.py) :
    sans ce header, GhostDesk interprète les coordonnées de clic en pixels
    écran natifs au lieu du repère normalisé 0-1000 utilisé par ces modèles,
    et les clics atterrissent à côté de leur cible.
    """
    import app.main as main_mod

    main_mod.SERVERS["desktop"] = {
        "transport": "http",
        "url": echo_http_server_with_model_space["url"],
        "token": echo_http_server_with_model_space["token"],
        "model_space": echo_http_server_with_model_space["model_space"],
    }

    resp = _client().post("/call", json={"tool": "echo", "arguments": {"message": "bonjour"}})
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "echo: bonjour"


def test_http_server_omits_model_space_header_when_unset(echo_http_server_rejecting_model_space_header):
    """
    GHOSTDESK_MODEL_SPACE="" (modèle frontière travaillant nativement en
    pixels écran, ex. Claude/GPT-4o) : le header ne doit JAMAIS être envoyé,
    pas seulement être absent de la config par défaut — server["model_space"]
    falsy (chaîne vide) doit empêcher tout ajout du header, voir
    _run_on_server dans app/main.py.
    """
    import app.main as main_mod

    main_mod.SERVERS["desktop"] = {
        "transport": "http",
        "url": echo_http_server_rejecting_model_space_header["url"],
        "token": echo_http_server_rejecting_model_space_header["token"],
        "model_space": "",
    }

    resp = _client().post("/call", json={"tool": "echo", "arguments": {"message": "bonjour"}})
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "echo: bonjour"


def test_http_server_wrong_token_fails(echo_http_server):
    import app.main as main_mod

    main_mod.SERVERS = {
        "desktop": {
            "transport": "http",
            "url": echo_http_server["url"],
            "token": "mauvais-token",
        },
    }

    resp = _client().get("/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == {}


def test_ocr_server_schema_exposed_and_callable(echo_http_server):
    """
    Le serveur "ocr" (services/ocr-service, find_text/read_screen) suit le
    même mécanisme que "desktop"/GhostDesk : connexion HTTP persistante
    plutôt qu'un conteneur spawné à la demande. Le faux serveur echo tient
    lieu d'ocr-service ici : ce test vérifie le câblage générique de
    mcp-client (registre, schéma, appel), pas la logique OCR elle-même
    (couverte par la suite de tests d'ocr-service).
    """
    import app.main as main_mod

    main_mod.SERVERS["ocr"] = {
        "transport": "http",
        "url": echo_http_server["url"],
        "token": echo_http_server["token"],
    }

    resp = _client().get("/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"]["echo"] == "ocr"

    resp = _client().get("/tools/schema")
    assert resp.status_code == 200
    names = [t["function"]["name"] for t in resp.json()["tools"]]
    assert "echo" in names

    resp = _client().post("/call", json={"tool": "echo", "arguments": {"message": "bonjour"}})
    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == "echo: bonjour"


def test_ocr_server_wrong_token_fails(echo_http_server):
    import app.main as main_mod

    main_mod.SERVERS = {
        "ocr": {
            "transport": "http",
            "url": echo_http_server["url"],
            "token": "mauvais-token",
        },
    }

    resp = _client().get("/tools")
    assert resp.status_code == 200
    assert resp.json()["tools"] == {}


class _FakeSession:
    def __init__(self, id_: int):
        self.id = id_


def _patch_open_session(main_mod, monkeypatch):
    """
    Remplace _open_session par une fabrique de sessions factices comptées :
    permet de vérifier QUI (persistant vs éphémère) rouvre une session sans
    dépendre d'un vrai serveur MCP ni du protocole réseau.
    """
    calls = {"n": 0}

    async def fake_open_session(server_name, stack):
        calls["n"] += 1
        return _FakeSession(calls["n"])

    monkeypatch.setattr(main_mod, "_open_session", fake_open_session)
    return calls


def test_persistent_session_reused_across_calls(monkeypatch):
    """
    Le serveur "browser" (Playwright) scope son état navigateur à la SESSION
    MCP, pas au process serveur (voir docs/resolved-bugs.md) : mcp-client doit donc réutiliser
    la même session entre deux appels d'outils plutôt que d'en rouvrir une à
    chaque fois, sans quoi l'état (page visitée...) serait perdu entre deux
    appels malgré un serveur HTTP persistant.
    """
    import app.main as main_mod

    main_mod.SERVERS = {
        "browser": {"transport": "http", "url": "http://unused", "token": "", "persistent_session": True},
    }
    main_mod._persistent_sessions.clear()
    main_mod._persistent_locks["browser"] = asyncio.Lock()
    calls = _patch_open_session(main_mod, monkeypatch)

    async def action(session):
        return session.id

    async def run():
        first = await main_mod._run_on_server("browser", action)
        second = await main_mod._run_on_server("browser", action)
        return first, second

    first, second = asyncio.run(run())
    assert first == second == 1
    assert calls["n"] == 1


def test_ephemeral_server_opens_new_session_per_call(monkeypatch):
    """Sans persistent_session, chaque appel doit garder son comportement d'origine : une session neuve à chaque fois."""
    import app.main as main_mod

    main_mod.SERVERS = {
        "desktop": {"transport": "http", "url": "http://unused", "token": ""},
    }
    calls = _patch_open_session(main_mod, monkeypatch)

    async def action(session):
        return session.id

    async def run():
        first = await main_mod._run_on_server("desktop", action)
        second = await main_mod._run_on_server("desktop", action)
        return first, second

    first, second = asyncio.run(run())
    assert (first, second) == (1, 2)
    assert calls["n"] == 2


def test_persistent_session_dropped_and_reopened_after_error(monkeypatch):
    """
    Si l'action échoue (session probablement morte, ex. serveur redémarré),
    la session en cache doit être jetée : le prochain appel doit en rouvrir
    une neuve plutôt que de rester bloqué sur une connexion cassée.
    """
    import app.main as main_mod

    main_mod.SERVERS = {
        "browser": {"transport": "http", "url": "http://unused", "token": "", "persistent_session": True},
    }
    main_mod._persistent_sessions.clear()
    main_mod._persistent_locks["browser"] = asyncio.Lock()
    calls = _patch_open_session(main_mod, monkeypatch)

    async def failing_action(session):
        raise RuntimeError("session cassée")

    async def ok_action(session):
        return session.id

    async def run():
        with pytest.raises(RuntimeError):
            await main_mod._run_on_server("browser", failing_action)
        return await main_mod._run_on_server("browser", ok_action)

    result = asyncio.run(run())
    assert result == 2  # nouvelle session rouverte, pas la première réutilisée
    assert calls["n"] == 2
    # la session rouverte (la 2e) est bien celle mise en cache pour le prochain appel
    assert main_mod._persistent_sessions["browser"][1].id == 2


# ─────────────────────────────────────────────────────────────────────────
# browser_extract (Phase 1d-révisée, voir docs/history.md "correctif extraction") :
# outil synthétique dispatché en interne vers browser_evaluate avec un
# template JS FIXE — le modèle ne fournit jamais de code, seulement un texte
# à chercher.
# ─────────────────────────────────────────────────────────────────────────

BROWSER_EVALUATE_ECHO_SERVER_PATH = Path(__file__).parent / "fixtures" / "browser_evaluate_echo_server.py"


def test_build_extract_function_embeds_query_as_escaped_json_string():
    """Fonction pure : la requête est interpolée via json.dumps (syntaxe de
    chaîne JSON = syntaxe de chaîne JS valide), jamais concaténée brute — un
    guillemet ou un backslash dans la requête ne peut donc jamais faire
    "s'échapper" du littéral de chaîne vers du code JS arbitraire."""
    import app.main as main_mod

    js = main_mod._build_extract_function('") ; alert(1); ("')
    assert json.dumps('") ; alert(1); ("') in js
    assert js.count("const query =") == 1
    assert "document.createTreeWalker" in js


@pytest.fixture
def browser_evaluate_echo_server():
    import app.main as main_mod

    main_mod.SERVERS = {
        "browser": {
            "transport": "stdio",
            "params": StdioServerParameters(
                command=sys.executable, args=[str(BROWSER_EVALUATE_ECHO_SERVER_PATH)]
            ),
        },
    }
    main_mod._tool_registry.clear()
    yield
    main_mod._tool_registry.clear()


def test_browser_extract_is_registered_when_browser_server_present(browser_evaluate_echo_server):
    resp = _client().get("/tools/schema")
    names = {t["function"]["name"] for t in resp.json()["tools"]}
    assert "browser_extract" in names
    assert "browser_evaluate" in names  # le vrai outil reste exposé tel quel, pas remplacé


def test_browser_extract_dispatches_to_browser_evaluate_with_fixed_template(browser_evaluate_echo_server):
    """Le serveur de test renvoie tel quel le JS reçu (voir
    browser_evaluate_echo_server.py) : permet de vérifier le template
    généré SANS dépendre d'un vrai navigateur."""
    import app.main as main_mod

    resp = _client().post("/call", json={"tool": "browser_extract", "arguments": {"query": "KX-4471"}})
    assert resp.status_code == 200
    text = resp.json()["content"][0]["text"]
    assert text == main_mod._build_extract_function("KX-4471")
    assert json.dumps("KX-4471") in text


# ─────────────────────────────────────────────────────────────────────────
# Mode bulk de browser_extract (trouvé en investiguant T1, voir
# BULK_CHECK_DIRECTIVE app/graph.py) : `urls` optionnel vérifie PLUSIEURS
# pages en un seul appel via fetch()+DOMParser (TIER_READ, aucun code fourni
# par le modèle) plutôt que la boucle browser_evaluate écrite par le modèle.
# ─────────────────────────────────────────────────────────────────────────


def test_build_extract_function_without_urls_returns_single_page_template():
    """Rétrocompatibilité stricte : sans `urls`, le template est
    inchangé — aucune régression du mode mono-page existant."""
    import app.main as main_mod

    assert main_mod._build_extract_function("KX-4471", None) == main_mod._build_extract_function("KX-4471")
    assert main_mod._build_extract_function("KX-4471", []) == main_mod._build_extract_function("KX-4471")


def test_build_extract_function_with_urls_embeds_query_and_urls_as_escaped_json():
    """Requête ET URL interpolées via json.dumps — même garantie
    d'échappement que le mode mono-page, étendue à un tableau."""
    import app.main as main_mod

    urls = ["http://catalog/product-1.html", '") ; alert(1); ("']
    js = main_mod._build_extract_function("KX-4471", urls)
    assert json.dumps("KX-4471") in js
    assert json.dumps(urls) in js
    assert "fetch(url)" in js
    assert "DOMParser" in js
    assert "async () =>" in js  # fetch() nécessite une fonction async, contrairement au mode mono-page


def test_browser_extract_dispatches_bulk_template_when_urls_provided(browser_evaluate_echo_server):
    import app.main as main_mod

    urls = ["http://catalog/product-1.html", "http://catalog/product-2.html"]
    resp = _client().post(
        "/call", json={"tool": "browser_extract", "arguments": {"query": "KX-4471", "urls": urls}}
    )
    assert resp.status_code == 200
    text = resp.json()["content"][0]["text"]
    assert text == main_mod._build_extract_function("KX-4471", urls)
    assert json.dumps(urls) in text


def test_browser_extract_schema_declares_optional_urls_array(browser_evaluate_echo_server):
    resp = _client().get("/tools/schema")
    tool = next(t for t in resp.json()["tools"] if t["function"]["name"] == "browser_extract")
    props = tool["function"]["parameters"]["properties"]
    assert props["urls"]["type"] == "array"
    assert tool["function"]["parameters"]["required"] == ["query"]  # urls reste optionnel


# ─────────────────────────────────────────────────────────────────────────
# POST /reset-session/{server_name} (Phase 1d-révisée, voir docs/history.md
# "isolation entre tâches") : purge une session persistante (état
# navigateur/onglets pour "browser") entre deux tâches du harnais.
# ─────────────────────────────────────────────────────────────────────────


def test_reset_session_drops_cache_and_next_call_reopens_fresh(monkeypatch):
    import app.main as main_mod

    main_mod.SERVERS = {
        "browser": {"transport": "http", "url": "http://unused", "token": "", "persistent_session": True},
    }
    main_mod._persistent_sessions.clear()
    main_mod._persistent_locks["browser"] = asyncio.Lock()
    calls = _patch_open_session(main_mod, monkeypatch)

    async def action(session):
        return session.id

    async def run():
        first = await main_mod._run_on_server("browser", action)
        return first

    first = asyncio.run(run())
    assert first == 1

    resp = _client().post("/reset-session/browser")
    assert resp.status_code == 200
    assert "browser" not in main_mod._persistent_sessions

    second = asyncio.run(run())
    assert second == 2  # nouvelle session rouverte, pas l'ancienne réutilisée
    assert calls["n"] == 2


def test_reset_session_unknown_server_is_404():
    main_mod_resp = _client().post("/reset-session/does-not-exist")
    assert main_mod_resp.status_code == 404


def test_reset_session_non_persistent_server_is_404():
    """echo (fixture par défaut) n'est pas configuré en session persistante :
    rien à réinitialiser, 404 plutôt qu'un no-op silencieux qui masquerait
    une faute de frappe côté appelant."""
    resp = _client().post("/reset-session/echo")
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────
# Stabilisation post-navigation (voir docs/history.md, investigation T10 :
# "désynchronisation snapshot/URL") : browser_wait_for appelé automatiquement
# après CHAQUE browser_navigate/browser_click réussi, transparent pour
# l'agent, avant que le prochain outil (browser_snapshot ou autre) ne voie
# le résultat.
# ─────────────────────────────────────────────────────────────────────────


class _RecordingSession:
    """Session factice : enregistre chaque appel (nom, arguments) et renvoie
    un résultat minimal, sans dépendre d'un vrai serveur MCP/navigateur."""

    def __init__(self, calls):
        self.calls = calls

    async def call_tool(self, name, arguments):
        from mcp.types import TextContent

        self.calls.append((name, arguments))

        class _Result:
            content = [TextContent(type="text", text=f"ok:{name}")]

        return _Result()


def _patch_run_on_server_recording(main_mod, monkeypatch):
    """Court-circuite _run_on_server (déjà testé séparément, voir plus haut)
    pour isoler la logique ajoutée dans call_tool() elle-même : une seule
    session factice enregistrant tous les appels, quel que soit le serveur
    visé."""
    calls = []
    session = _RecordingSession(calls)

    async def fake_run_on_server(server_name, action):
        return await action(session)

    monkeypatch.setattr(main_mod, "_run_on_server", fake_run_on_server)
    return calls


def _register_fake_browser_tool(main_mod, name, required=None):
    main_mod._tool_registry[name] = {
        "server": "browser",
        "description": "",
        "inputSchema": {"type": "object", "properties": {}, "required": required or []},
    }


def test_browser_navigate_triggers_stabilization_wait(monkeypatch):
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_navigate", ["url"])
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post("/call", json={"tool": "browser_navigate", "arguments": {"url": "https://exemple.com"}})

    assert resp.status_code == 200
    assert calls == [
        ("browser_navigate", {"url": "https://exemple.com"}),
        ("browser_wait_for", {"time": main_mod.BROWSER_STABILIZE_WAIT_SECONDS}),
    ]


def test_browser_click_triggers_stabilization_wait(monkeypatch):
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_click", ["target"])
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post("/call", json={"tool": "browser_click", "arguments": {"target": "e1"}})

    assert resp.status_code == 200
    assert calls[-1] == ("browser_wait_for", {"time": main_mod.BROWSER_STABILIZE_WAIT_SECONDS})


def test_browser_snapshot_does_not_trigger_stabilization_wait(monkeypatch):
    """Seuls navigate/click déclenchent le délai — un simple snapshot ne
    change pas la page, rien à stabiliser derrière lui."""
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_snapshot")
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post("/call", json={"tool": "browser_snapshot", "arguments": {}})

    assert resp.status_code == 200
    assert calls == [("browser_snapshot", {})]


def test_stabilization_wait_disabled_via_zero_seconds(monkeypatch):
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_navigate", ["url"])
    monkeypatch.setattr(main_mod, "BROWSER_STABILIZE_WAIT_SECONDS", 0.0)
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post("/call", json={"tool": "browser_navigate", "arguments": {"url": "https://exemple.com"}})

    assert resp.status_code == 200
    assert calls == [("browser_navigate", {"url": "https://exemple.com"})]


# ─────────────────────────────────────────────────────────────────────────
# Normalisation ref= (2026-07-31, voir docs/resolved-bugs.md "défaut ref=
# browser_fill_form") : le modèle recopie souvent l'annotation "[ref=e7]"
# de browser_snapshot telle quelle comme valeur de `target`, alors que
# Playwright n'accepte que le jeton nu ("e7") — "ref=e7" est alors
# interprété comme un moteur de sélecteur inconnu ("ref") et échoue à
# 100% (mesuré sur l'historique complet des appels browser_fill_form).
# ─────────────────────────────────────────────────────────────────────────


def test_normalize_ref_value_strips_ref_prefix():
    import app.main as main_mod

    assert main_mod._normalize_ref_value("ref=e7") == "e7"
    assert main_mod._normalize_ref_value("ref=f2e7") == "f2e7"


def test_normalize_ref_value_leaves_other_forms_untouched():
    import app.main as main_mod

    assert main_mod._normalize_ref_value("e7") == "e7"
    assert main_mod._normalize_ref_value("input[name='username']") == "input[name='username']"
    assert main_mod._normalize_ref_value("ref=not-a-ref") == "ref=not-a-ref"
    assert main_mod._normalize_ref_value(None) is None


def test_normalize_ref_targets_covers_nested_fill_form_fields():
    """browser_fill_form porte ses targets dans un tableau `fields` — la
    normalisation doit s'y appliquer sans traitement spécial à ce nom
    d'outil (couvre tout outil futur avec la même forme imbriquée)."""
    import app.main as main_mod

    arguments = {
        "fields": [
            {"target": "ref=e7", "name": "Référence produit", "type": "textbox", "value": "PX-2007"},
            {"target": "ref=e9", "name": "Nouveau niveau de stock", "type": "textbox", "value": "12"},
        ]
    }
    normalized = main_mod._normalize_ref_targets(arguments)
    assert normalized["fields"][0]["target"] == "e7"
    assert normalized["fields"][1]["target"] == "e9"
    # les autres clés (name, value...) ne sont jamais touchées
    assert normalized["fields"][0]["value"] == "PX-2007"


def test_normalize_ref_targets_covers_drag_start_end_target():
    import app.main as main_mod

    normalized = main_mod._normalize_ref_targets({"startTarget": "ref=e3", "endTarget": "ref=e5"})
    assert normalized == {"startTarget": "e3", "endTarget": "e5"}


def test_call_tool_normalizes_ref_target_before_dispatch(monkeypatch):
    """Bout en bout via /call : browser_click envoyé avec "ref=e1" doit
    atteindre le serveur MCP réel sous la forme nue "e1"."""
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_click", ["target"])
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post("/call", json={"tool": "browser_click", "arguments": {"target": "ref=e1"}})

    assert resp.status_code == 200
    assert calls[0] == ("browser_click", {"target": "e1"})


def test_call_tool_normalizes_nested_fill_form_targets_before_dispatch(monkeypatch):
    import app.main as main_mod

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_fill_form", ["fields"])
    calls = _patch_run_on_server_recording(main_mod, monkeypatch)

    resp = _client().post(
        "/call",
        json={
            "tool": "browser_fill_form",
            "arguments": {
                "fields": [{"target": "ref=e7", "name": "Référence produit", "type": "textbox", "value": "PX-2007"}]
            },
        },
    )

    assert resp.status_code == 200
    assert calls[0][1]["fields"][0]["target"] == "e7"


def test_call_tool_rewrites_unknown_ref_engine_error_into_friendly_message(monkeypatch):
    """Filet pour toute forme non normalisée en amont : le message brut de
    Playwright ("Unknown engine...") est remplacé par une redirection
    exploitable par le modèle, jamais un blocage sec."""
    import app.main as main_mod
    from mcp.types import TextContent

    main_mod._tool_registry.clear()
    _register_fake_browser_tool(main_mod, "browser_click", ["target"])

    class _ErrorSession:
        async def call_tool(self, name, arguments):
            class _Result:
                content = [TextContent(type="text", text='Error: browserBackend.callTool: Unknown engine "ref" while parsing selector ref=e7')]

            return _Result()

    async def fake_run_on_server(server_name, action):
        return await action(_ErrorSession())

    monkeypatch.setattr(main_mod, "_run_on_server", fake_run_on_server)

    resp = _client().post("/call", json={"tool": "browser_click", "arguments": {"target": "ref=e7"}})

    assert resp.status_code == 200
    assert resp.json()["content"][0]["text"] == main_mod._FRIENDLY_REF_ERROR


# ─────────────────────────────────────────────────────────────────────────
# browser_inspect (2026-07-31, voir docs/resolved-bugs.md "défaut ref=
# browser_fill_form") : capacité de fond manquante identifiée en creusant
# la brèche B-β hard — le repli légitime d'introspection DOM passait par
# browser_evaluate (NEVER_GRANTABLE) faute d'alternative TIER_READ. Même
# mouvement que browser_extract : template JS FIXE, jamais de code fourni
# par le modèle.
# ─────────────────────────────────────────────────────────────────────────


def test_build_inspect_call_without_target_scans_all_form_fields():
    import app.main as main_mod

    tool, args = main_mod._build_inspect_call()
    assert tool == "browser_evaluate"
    assert args == {"function": main_mod._BROWSER_INSPECT_JS_ALL}


def test_build_inspect_call_with_target_normalizes_ref_and_scopes_evaluate():
    import app.main as main_mod

    tool, args = main_mod._build_inspect_call("ref=e7", "champ référence produit")
    assert tool == "browser_evaluate"
    assert args == {
        "function": main_mod._BROWSER_INSPECT_JS_SINGLE,
        "target": "e7",
        "element": "champ référence produit",
    }


def test_browser_inspect_is_registered_when_browser_server_present(browser_evaluate_echo_server):
    resp = _client().get("/tools/schema")
    names = {t["function"]["name"] for t in resp.json()["tools"]}
    assert "browser_inspect" in names


def test_browser_inspect_dispatches_to_browser_evaluate_with_fixed_template(browser_evaluate_echo_server):
    """Le serveur de test renvoie tel quel le JS reçu — vérifie le
    dispatch SANS dépendre d'un vrai navigateur."""
    import app.main as main_mod

    resp = _client().post("/call", json={"tool": "browser_inspect", "arguments": {}})
    assert resp.status_code == 200
    text = resp.json()["content"][0]["text"]
    assert text == main_mod._BROWSER_INSPECT_JS_ALL


def test_browser_inspect_with_target_dispatches_single_element_template(browser_evaluate_echo_server):
    """Le serveur de test-écho ne renvoie que `function` (voir
    browser_evaluate_echo_server.py) : cette limite du fixture suffit à
    vérifier le template choisi, pas la normalisation de `target` (déjà
    couverte par test_build_inspect_call_with_target_normalizes_ref_...)."""
    import app.main as main_mod

    resp = _client().post("/call", json={"tool": "browser_inspect", "arguments": {"target": "ref=e7"}})
    assert resp.status_code == 200
    text = resp.json()["content"][0]["text"]
    assert text == main_mod._BROWSER_INSPECT_JS_SINGLE
