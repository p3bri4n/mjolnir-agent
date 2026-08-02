"""
Visual-channel feasibility probe (docs/briefs/B5-security-hardening.md,
Phase 3 point 3 — GhostDesk removal decision): 8 minimal fixtures, one per
content-rendering pattern that could plausibly force a visual (screenshot
+ OCR) read instead of a DOM read. Session-technique tooling, not part of
any frozen benchmark (docs/benchmark-v1.md/v2.md) — no task_id, no
assertion wired into test_web_tasks*.py, never measured as agent capability.

Each page carries ONE ground-truth string, `VP-100N`, checked directly
(browser_snapshot / browser_extract / screenshot+OCR), never through the
agent loop. Where the point is to test a channel that should NOT expose
DOM text (canvas, WebGL, image, PDF), the string is baked into a PNG/PDF
at BUILD time — never a JS/HTML literal — same defeat-proof method as
family E's E2 (see fixtures/perception/generate_perception.py): a
TreeWalker-based tool (browser_extract) walks every text node under
document.body INCLUDING inline <script> tag contents, so any literal
string in a setup script would leak regardless of the rendering channel
actually used.

Usage: python3 generate_visual_probe.py <output_dir>
"""

import struct
import sys
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VP1_CANVAS2D = "VP-1001"
VP2_WEBGL = "VP-1002"
VP3_IMAGE = "VP-1003"
VP4_PDF = "VP-1004"
# VP5 (iframe cross-origin) has no code of its own: ground truth is the
# EXISTING, unmodified fixture-docs title ("Documentation — Sommaire") —
# reusing a real different-origin fixture rather than standing up a
# second throwaway container, and never touching the frozen docs fixture.
VP6_SHADOW_DOM = "VP-1006"
VP7_SVG_TEXT = "VP-1007"
VP8_OFFVIEWPORT = "VP-1008"

PAGE_TEMPLATE = """<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""


def _render_text_png(path: Path, text: str, size=(320, 100)) -> None:
    img = Image.new("RGB", size, color="white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, size[1] // 2 - 18), text, fill="black", font=font)
    img.save(path)


def _minimal_pdf_bytes(text: str) -> bytes:
    """Hand-built minimal single-page PDF (no external library): a
    Helvetica Tj string is the ONLY place `text` appears, inside the
    content stream — nothing HTML/JS-side to leak it via browser_extract,
    since this file is never parsed as HTML."""
    content_stream = f"BT /F1 24 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 150] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content_stream)).encode() + b" >>\nstream\n" + content_stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode()
    )
    return bytes(out)


# VP1 — Canvas 2D: the PNG is drawn onto a <canvas> via drawImage() at
# runtime. The canvas element itself carries no text node and no
# accessible name; only the rendered pixels show VP-1001.
VP1_BODY = """
<p>Le tableau ci-dessous affiche un code de suivi.</p>
<canvas id="c1" width="320" height="100"></canvas>
<script>
  const img = new Image();
  img.onload = () => document.getElementById('c1').getContext('2d').drawImage(img, 0, 0);
  img.src = 'vp1-source.png';
</script>
"""

# VP2 — WebGL: the SAME kind of pre-rendered PNG, uploaded as a texture
# and rendered onto a full-canvas textured quad. Deliberately minimal
# (no lighting, no 3D): the point is the RENDERING PATH (WebGL draw
# call, no DOM/accessibility representation of the result), not a
# realistic 3D scene.
VP2_BODY = """
<p>Rendu WebGL ci-dessous.</p>
<canvas id="c2" width="320" height="100"></canvas>
<script>
(function() {
  const canvas = document.getElementById('c2');
  const gl = canvas.getContext('webgl');
  const vs = gl.createShader(gl.VERTEX_SHADER);
  gl.shaderSource(vs, 'attribute vec2 p; varying vec2 uv; void main(){ uv = (p+1.0)/2.0; uv.y = 1.0 - uv.y; gl_Position = vec4(p,0,1); }');
  gl.compileShader(vs);
  const fs = gl.createShader(gl.FRAGMENT_SHADER);
  gl.shaderSource(fs, 'precision mediump float; varying vec2 uv; uniform sampler2D tex; void main(){ gl_FragColor = texture2D(tex, uv); }');
  gl.compileShader(fs);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const tex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, tex);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([255,255,255,255]));
  const img = new Image();
  img.onload = () => {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  };
  img.src = 'vp2-source.png';
})();
</script>
"""

# VP3 — plain image (control for the "capture-only" group, same pattern
# as family E's E2): alt="" deliberately empty.
VP3_BODY = """
<p>Fiche visuelle ci-dessous.</p>
<img src="vp3-source.png" alt="" width="320" height="100">
"""

# VP4 — PDF opened directly (Chromium's built-in viewer becomes the top-
# level document): tested by navigating straight to the .pdf URL, not by
# embedding it in an HTML wrapper — the most representative case of "a
# PDF the agent is looking at."
VP4_LINK_BODY = """
<p>Document PDF : <a href="vp4-document.pdf">vp4-document.pdf</a></p>
"""

# VP6 — shadow DOM (open root): the text is fetched from a plain-text
# file at runtime, never a literal in the setup script, so it cannot leak
# via browser_extract's script-tag text-node quirk (see module docstring)
# regardless of whether shadow content itself is walked.
VP6_BODY = """
<p>Composant à shadow DOM ouvert ci-dessous.</p>
<div id="host"></div>
<script>
(function() {
  const host = document.getElementById('host');
  const root = host.attachShadow({mode: 'open'});
  fetch('vp6-shadow-text.txt').then(r => r.text()).then(t => {
    const p = document.createElement('p');
    p.textContent = t.trim();
    root.appendChild(p);
  });
})();
</script>
"""

VP7_BODY = f"""
<p>Texte SVG ci-dessous (cas de contrôle : attendu lisible en DOM, aucune capture nécessaire).</p>
<svg width="320" height="60" xmlns="http://www.w3.org/2000/svg">
  <text x="10" y="35" font-size="24">{VP7_SVG_TEXT}</text>
</svg>
"""

VP8_BODY = f"""
<p>Contenu hors viewport ci-dessous (texte DOM réel, jamais rendu à l'écran).</p>
<div style="position:absolute; left:-9999px; top:-9999px;" id="offviewport">{VP8_OFFVIEWPORT}</div>
"""

VP5_BODY = """
<p>Iframe cross-origin ci-dessous (pointe vers fixture-docs, origine différente, jamais modifiée).</p>
<iframe id="cross-origin-frame" src="http://fixture-docs/docs/index.html" width="400" height="200"></iframe>
"""


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _render_text_png(out_dir / "vp1-source.png", VP1_CANVAS2D)
    _render_text_png(out_dir / "vp2-source.png", VP2_WEBGL)
    _render_text_png(out_dir / "vp3-source.png", VP3_IMAGE)
    (out_dir / "vp4-document.pdf").write_bytes(_minimal_pdf_bytes(VP4_PDF))
    (out_dir / "vp6-shadow-text.txt").write_text(VP6_SHADOW_DOM, encoding="utf-8")

    pages = {
        "vp1-canvas2d": (VP1_CANVAS2D, VP1_BODY),
        "vp2-webgl": (VP2_WEBGL, VP2_BODY),
        "vp3-image": (VP3_IMAGE, VP3_BODY),
        "vp4-pdf": (VP4_PDF, VP4_LINK_BODY),
        "vp5-iframe-cross-origin": ("Documentation — Sommaire", VP5_BODY),
        "vp6-shadow-dom": (VP6_SHADOW_DOM, VP6_BODY),
        "vp7-svg-text": (VP7_SVG_TEXT, VP7_BODY),
        "vp8-offviewport": (VP8_OFFVIEWPORT, VP8_BODY),
    }
    index_links = []
    for slug, (_ground_truth, body) in pages.items():
        html = PAGE_TEMPLATE.format(title=f"Sonde visuelle — {slug}", body=body)
        (out_dir / f"{slug}.html").write_text(html, encoding="utf-8")
        index_links.append(f'<li><a href="{slug}.html">{slug}</a></li>')

    index_html = PAGE_TEMPLATE.format(
        title="Sonde de faisabilité canal visuel — index",
        body="<ul>" + "".join(index_links) + "</ul>",
    )
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")


if __name__ == "__main__":
    generate(Path(sys.argv[1]))
