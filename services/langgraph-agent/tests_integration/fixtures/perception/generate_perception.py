"""
Benchmark v2, famille E (canaux de perception, docs/briefs/B3-benchmark-v2.md).
Trois pages statiques, une valeur figée par page, chacune conçue pour
n'être lisible que par UN SEUL canal (E1 : DOM seul, E2 : capture seule)
ou par les deux indifféremment (E3 : équivalence, juge économique — quel
canal l'agent utilise-t-il en premier).

Usage : python3 generate_perception.py <dossier_de_sortie>
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

E1_VALUE = "ZK-3391"
E2_VALUE = "ZK-3392"
E3_VALUE = "ZK-3393"

PAGE_TEMPLATE = """<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""

# E1 : le code est un vrai nœud de texte DOM, donc présent dans l'arbre
# d'accessibilité (browser_snapshot) — mais positionné hors-écran
# (position:absolute, décalage négatif énorme), donc absent de toute
# capture de la zone visible (browser_take_screenshot).
E1_BODY = """
<p>Merci de consulter les détails ci-dessous.</p>
<div style="position:absolute; left:-9999px; top:-9999px;" id="hidden-code">
Code interne : ZK-3391
</div>
"""

# E2 : le texte est un PNG pré-rendu au BUILD (jamais de JS côté client
# qui le dessinerait au runtime). Deux essais précédents ont fui la
# valeur sans aucune perception visuelle (live-verified 2026-07-30, "B3
# SLICE 10") : (1) la chaîne en clair dans un <script> canvas.fillText(),
# lisible par le TreeWalker(SHOW_TEXT) de browser_extract, qui parcourt
# TOUT nœud texte de document.body y compris le contenu littéral d'une
# balise <script> ; (2) une fois la chaîne remplacée par des codes de
# caractères calculés au runtime, browser_evaluate("() =>
# document.documentElement.innerHTML") a quand même exposé le tableau de
# codes en clair dans le code source, que le modèle a pu décoder par
# simple raisonnement textuel — aucun pixel jamais perçu. Un PNG statique
# élimine les deux : aucune représentation textuelle/calculable de la
# valeur n'existe nulle part dans le HTML/JS servi, seul le rendu des
# pixels du fichier image la révèle. alt="" délibérément vide (un texte
# alternatif remettrait la valeur dans l'arbre d'accessibilité, exactement
# ce qu'E2 doit exclure).
E2_IMAGE_FILENAME = "e2-code.png"
E2_BODY = f"""
<p>Cette fiche affiche une information visuelle ci-dessous.</p>
<img src="{E2_IMAGE_FILENAME}" alt="" width="320" height="80">
"""


def _render_e2_image(out_dir: Path) -> None:
    img = Image.new("RGB", (320, 80), color="white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 25), f"Code interne : {E2_VALUE}", fill="black", font=font)
    img.save(out_dir / E2_IMAGE_FILENAME)

# E3 : texte visible normalement — les deux canaux le trouvent, seul le
# CHOIX du canal (et son coût) est mesuré, jamais la correction.
E3_BODY = """
<p>Code interne : ZK-3393</p>
"""


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _render_e2_image(out_dir)
    pages = {
        "e1-offviewport": PAGE_TEMPLATE.format(title="Fiche produit — E1", body=E1_BODY),
        "e2-canvas": PAGE_TEMPLATE.format(title="Fiche produit — E2", body=E2_BODY),
        "e3-equivalence": PAGE_TEMPLATE.format(title="Fiche produit — E3", body=E3_BODY),
    }
    for slug, html in pages.items():
        (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
