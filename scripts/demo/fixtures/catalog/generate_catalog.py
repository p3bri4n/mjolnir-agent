"""
Demo-only catalog fixture generator. NOT the benchmark fixture (see
services/langgraph-agent/tests_integration/fixtures/catalog/ for that,
frozen and hashed) -- this is a small, styled, deterministic duplicate
built solely for scripts/record-demo.sh. Never referenced by
tests_integration/, never hashed, free to change.

6 products, 2 pages of 3, name + link only on the list (price/reference
on the product page) -- same navigation shape as the real fixture, small
enough to read at a glance in a 25s recording. KX-4471 sits on page 2,
the exact detail the demo task asks the agent to find.

Usage: python3 generate_catalog.py <output_dir>
"""
import sys
from pathlib import Path

PER_PAGE = 3

PRODUCTS = [
    {"i": 1, "name": "Lampe de bureau Compacte", "reference": "PX-1001", "price": "29.90", "stock": 40},
    {"i": 2, "name": "Sacoche Robuste", "reference": "PX-1002", "price": "54.00", "stock": 8},
    {"i": 3, "name": "Clavier Silencieux", "reference": "PX-1003", "price": "72.50", "stock": 15},
    {"i": 4, "name": "Chaise Ergonomique", "reference": "KX-4471", "price": "84.90", "stock": 12},
    {"i": 5, "name": "Étagère Modulaire", "reference": "PX-1005", "price": "112.00", "stock": 3},
    {"i": 6, "name": "Casque Premium", "reference": "PX-1006", "price": "159.90", "stock": 22},
]

PAGE_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"><title>Catalogue — page {page}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><h1>Catalogue produits</h1></header>
<main>
<ul class="product-list">
{items}
</ul>
<nav class="pager">
{nav}
</nav>
</main>
</body>
</html>
"""

PRODUCT_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8"><title>{name}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><h1>{name}</h1></header>
<main>
<dl class="product-detail">
<dt>Référence</dt><dd id="reference">{reference}</dd>
<dt>Prix</dt><dd id="price">{price} €</dd>
<dt>Stock</dt><dd id="stock">{stock}</dd>
</dl>
<a class="back-link" href="/catalog/page-{page}.html">&larr; Retour au catalogue</a>
</main>
</body>
</html>
"""


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    n_pages = (len(PRODUCTS) + PER_PAGE - 1) // PER_PAGE
    for p in PRODUCTS:
        page = (p["i"] - 1) // PER_PAGE + 1
        html = PRODUCT_TEMPLATE.format(
            name=p["name"], reference=p["reference"], price=p["price"],
            stock=p["stock"], page=page,
        )
        (out_dir / f"product-{p['i']}.html").write_text(html, encoding="utf-8")

    for page in range(1, n_pages + 1):
        page_products = [p for p in PRODUCTS if (p["i"] - 1) // PER_PAGE + 1 == page]
        items = "\n".join(
            f'<li class="product-item"><a href="/catalog/product-{p["i"]}.html">{p["name"]}</a></li>'
            for p in page_products
        )
        nav_links = []
        if page > 1:
            nav_links.append(f'<a href="/catalog/page-{page - 1}.html">&larr; Précédent</a>')
        if page < n_pages:
            nav_links.append(f'<a href="/catalog/page-{page + 1}.html">Suivant &rarr;</a>')
        nav = " ".join(nav_links)
        html = PAGE_TEMPLATE.format(page=page, items=items, nav=nav)
        (out_dir / f"page-{page}.html").write_text(html, encoding="utf-8")

    index_html = (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/style.css">'
        "<title>Catalogue</title></head><body>"
        '<header><h1>Catalogue produits</h1></header>'
        '<main><p><a href="/catalog/page-1.html">Voir le catalogue</a></p></main>'
        "</body></html>"
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")


if __name__ == "__main__":
    generate(Path(sys.argv[1] if len(sys.argv) > 1 else "site"))
