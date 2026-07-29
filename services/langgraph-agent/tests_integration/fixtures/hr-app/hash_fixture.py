"""
Freeze guarantee for the hr-app fixture (T2/T3/T5/T6 — docs/benchmark-v1.md;
reused verbatim as family F of docs/briefs/B3-benchmark-v2.md).

catalog/docs (generate_catalog.py/generate_docs.py) hash their GENERATED
static HTML output — hr-app has no such output: content is served
dynamically by Flask (app.py) reading hr_data.py, with no other template
files (see app.py, no render_template/separate templates). The freeze
guarantee here is therefore over the two SOURCE files that fully determine
everything served: app.py (routes/logic) and hr_data.py (ground truth data).
Run after any change to either file:

    python3 fixtures/hr-app/hash_fixture.py
"""
import hashlib
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent
HASHED_FILES = ["app.py", "hr_data.py"]


def compute_hash(fixture_dir: Path = FIXTURE_DIR) -> str:
    sha = hashlib.sha256()
    for name in HASHED_FILES:
        sha.update(name.encode())
        sha.update((fixture_dir / name).read_bytes())
    return f"sha256:{sha.hexdigest()}"


if __name__ == "__main__":
    (FIXTURE_DIR / "HASHES.txt").write_text(compute_hash() + "\n", encoding="utf-8")
