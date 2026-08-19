"""
Demo-only admin fixture. NOT the benchmark fixture (see
services/langgraph-agent/tests_integration/fixtures/admin/ for that,
frozen and hashed) -- a small, styled duplicate built solely for
scripts/record-demo.sh. Never referenced by tests_integration/, never
hashed, free to change. Same shape as the real one (a stock-update form,
POST-only, no auth) so the agent's real approval-tier behavior (COMMIT
action -> human approval) is exercised identically.
"""
from flask import Flask, redirect, request, send_from_directory, url_for

app = Flask(__name__)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><h1>Panneau admin</h1></header>
<main>
<h2>{title}</h2>
{body}
</main>
</body>
</html>
"""


STOCK_FORM_HTML = """
<form id="stock-form" method="post" action="/stock/update">
  <label>Référence produit
    <input type="text" name="product_reference" required>
  </label>
  <label>Nouveau niveau de stock
    <input type="number" name="new_stock" required>
  </label>
  <button type="submit">Mettre à jour</button>
</form>
"""


@app.get("/")
def index():
    return redirect(url_for("stock_form"))


@app.get("/stock")
def stock_form():
    return _page("Mise à jour du stock", STOCK_FORM_HTML)


@app.post("/stock/update")
def stock_update():
    reference = request.form.get("product_reference", "")
    new_stock = request.form.get("new_stock", "0")
    body = (
        f'<p class="confirmation">Stock mis à jour pour '
        f'<strong>{reference}</strong> : nouveau niveau '
        f'<strong>{new_stock}</strong>.</p>'
    )
    return _page("Stock mis à jour", body)


@app.get("/style.css")
def style():
    return send_from_directory(".", "style.css")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
