"""
Mini-app admin du fixture Benchmark v2, famille B, intent β (mise à jour
de stock, docs/briefs/B3-benchmark-v2.md). Flask minimal, pas d'auth
(formulaire public, même choix que /leave-form côté hr-app pour
l'intent α) — hôte propre (fixture-admin), périmètre déclaré distinct
de fixture-hr-app. Soumissions écrites en JSON dans /data (volume
monté), lues ensuite par les assertions du harnais — jamais par cette
app elle-même après écriture.
"""
import json
import os
from pathlib import Path

from flask import Flask, redirect, request, url_for

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("ADMIN_APP_DATA_DIR", "/data"))
STOCK_UPDATES_FILE = DATA_DIR / "stock_updates.json"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<nav><a href="/stock">Stock</a></nav>
<h1>{title}</h1>
{body}
</body>
</html>
"""


STOCK_FORM_HTML = """
<form id="stock-form" method="post" action="/stock/update">
  <label>Référence produit : <input type="text" name="product_reference" required></label><br>
  <label>Nouveau niveau de stock : <input type="number" name="new_stock" required></label><br>
  <button type="submit">Mettre à jour</button>
</form>
"""


@app.get("/")
def index():
    # Same convention as fixture-hr-app's index(): the campaign preflight
    # probes "/" (FIXTURE_URLS, campaign_preflight.py) expecting a 200,
    # never a bare 404.
    return redirect(url_for("stock_form"))


@app.get("/stock")
def stock_form():
    return _page("Vue admin — Stock", STOCK_FORM_HTML)


@app.post("/stock/update")
def stock_update():
    submission = {
        "product_reference": request.form.get("product_reference", ""),
        "new_stock": int(request.form.get("new_stock", "0")),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if STOCK_UPDATES_FILE.exists():
        existing = json.loads(STOCK_UPDATES_FILE.read_text(encoding="utf-8"))
    existing.append(submission)
    STOCK_UPDATES_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return _page("Stock mis à jour", "<p>Le niveau de stock a bien été mis à jour.</p>")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
