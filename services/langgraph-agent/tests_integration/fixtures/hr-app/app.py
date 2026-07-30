"""
Mini-app RH du fixture Benchmark 0 (T2/T3/T5/T6 — voir docs/benchmark-v1.md).
Flask minimal, données figées dans hr_data.py. Les soumissions du
formulaire de congé (T2) sont écrites en JSON dans /data (volume monté),
lu ensuite par les assertions du harnais de test — jamais par cette app
elle-même après écriture.

Benchmark v2, famille A : /contacts (A3 — ambiguïté à résoudre, deux
candidats RH sous le même intitulé). /special-request (A4 — horizon
long/compaction stress) — formulaire final d'un parcours guidé
cross-sites, même mécanique d'écriture JSON que /leave-form.
"""
import csv
import io
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, Response, redirect, request, session, url_for

import hr_data

app = Flask(__name__)
app.secret_key = os.environ.get("HR_APP_SECRET_KEY", "fixture-not-for-prod")
app.permanent_session_lifetime = timedelta(
    seconds=int(os.environ.get("HR_APP_SESSION_TIMEOUT_SECONDS", "90"))
)

DATA_DIR = Path(os.environ.get("HR_APP_DATA_DIR", "/data"))
SUBMISSIONS_FILE = DATA_DIR / "leave_submissions.json"
# A4 (long-horizon/compaction stress, docs/briefs/B3-benchmark-v2.md) —
# same write-once, read-by-the-harness-only pattern as SUBMISSIONS_FILE.
SPECIAL_REQUEST_FILE = DATA_DIR / "special_requests.json"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<nav>
<a href="/employees">Employés</a> | <a href="/leave-form">Demande de congé</a> |
<a href="/leave-requests">Suivi des congés</a> | <a href="/contacts">Contacts</a> |
<a href="/special-request">Demande spéciale</a> | <a href="/export/employees.csv">Export CSV</a>
</nav>
<h1>{title}</h1>
{body}
</body>
</html>
"""


@app.get("/")
def index():
    return redirect(url_for("employees"))


@app.get("/employees")
def employees():
    rows = "\n".join(
        f'<tr data-id="{e["id"]}" data-department="{e["department"]}" data-salary="{e["salary"]}">'
        f'<td class="name">{e["name"]}</td><td class="department">{e["department"]}</td>'
        f'<td class="salary">{e["salary"]}</td></tr>'
        for e in hr_data.EMPLOYEES
    )
    departments = sorted({e["department"] for e in hr_data.EMPLOYEES})
    dept_options = "\n".join(f'<option value="{d}">{d}</option>' for d in departments)
    body = f"""
<label>Filtrer par département :
  <select id="dept-filter">
    <option value="">Tous</option>
    {dept_options}
  </select>
</label>
<button id="sort-salary">Trier par salaire (décroissant)</button>
<table id="employee-table">
<thead><tr><th>Nom</th><th>Département</th><th>Salaire</th></tr></thead>
<tbody id="employee-tbody">
{rows}
</tbody>
</table>
<script>
function currentRows() {{
  return Array.from(document.querySelectorAll('#employee-tbody tr'));
}}
document.getElementById('sort-salary').addEventListener('click', () => {{
  const tbody = document.getElementById('employee-tbody');
  const rows = currentRows();
  rows.sort((a, b) => Number(b.dataset.salary) - Number(a.dataset.salary));
  rows.forEach(r => tbody.appendChild(r));
}});
document.getElementById('dept-filter').addEventListener('change', (ev) => {{
  const dept = ev.target.value;
  currentRows().forEach(r => {{
    r.style.display = (!dept || r.dataset.department === dept) ? '' : 'none';
  }});
}});
</script>
"""
    return _page("Employés", body)


@app.get("/export/employees.csv")
def export_employees_csv():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["id", "name", "department", "salary"])
    writer.writeheader()
    for e in hr_data.EMPLOYEES:
        writer.writerow(e)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=employees.csv"},
    )


LEAVE_FORM_HTML = """
<form id="leave-form" method="post" action="/leave-form/submit">
  <label>Nom de l'employé : <input type="text" name="employee_name" required></label><br>
  <label>Date de début : <input type="date" name="start_date" required></label><br>
  <label>Date de fin : <input type="date" name="end_date" required></label><br>
  <label>Motif :
    <select name="reason" required>
      <option value="">Choisir...</option>
      <option value="conges_annuels">Congés annuels</option>
      <option value="maladie">Maladie</option>
      <option value="autre">Autre</option>
    </select>
  </label><br>
  <label>Urgent : <input type="checkbox" name="urgent"></label><br>
  <button type="submit">Envoyer</button>
</form>
<script>
document.getElementById('leave-form').addEventListener('submit', (ev) => {
  const start = ev.target.start_date.value;
  const end = ev.target.end_date.value;
  if (start && end && end < start) {
    ev.preventDefault();
    alert('La date de fin doit être après la date de début.');
  }
});
</script>
"""


@app.get("/leave-form")
def leave_form():
    return _page("Demande de congé", LEAVE_FORM_HTML)


@app.post("/leave-form/submit")
def leave_form_submit():
    submission = {
        "employee_name": request.form.get("employee_name", ""),
        "start_date": request.form.get("start_date", ""),
        "end_date": request.form.get("end_date", ""),
        "reason": request.form.get("reason", ""),
        "urgent": request.form.get("urgent") == "on",
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if SUBMISSIONS_FILE.exists():
        existing = json.loads(SUBMISSIONS_FILE.read_text(encoding="utf-8"))
    existing.append(submission)
    SUBMISSIONS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return _page("Demande envoyée", "<p>Votre demande de congé a bien été enregistrée.</p>")


SPECIAL_REQUEST_FORM_HTML = """
<form id="special-request-form" method="post" action="/special-request/submit">
  <label>Nom de l'employé : <input type="text" name="employee_name" required></label><br>
  <label>Référence produit (catalogue) : <input type="text" name="product_reference" required></label><br>
  <label>Valeur max_retry_delay (documentation) : <input type="text" name="max_retry_delay_value" required></label><br>
  <label>Nom (3e salaire Ingénierie) : <input type="text" name="engineering_third_salary_name" required></label><br>
  <button type="submit">Envoyer</button>
</form>
"""


@app.get("/special-request")
def special_request_form():
    """
    Benchmark v2, famille A (A4 — horizon long/compaction stress,
    docs/briefs/B3-benchmark-v2.md). Les 4 valeurs demandées doivent être
    RÉCOLTÉES ailleurs (catalogue, documentation, page /employees) avant
    de soumettre ce formulaire — dernière étape d'un parcours guidé
    cross-sites, même mécanique de vérité terrain que /leave-form (T2).

    Un ajout à 6 champs (masse salariale Ventes + congés en attente) a été
    tenté puis retiré (docs/history.md "B3 SLICE 7") : reproductiblement
    0/3 en direct, MAX_TOOL_ITERATIONS(20) atteint avant la soumission —
    fiabilité (3/3 à 4 champs) préférée à l'atteinte du seuil de 60
    messages visé par le brief, décision de checkpoint.
    """
    return _page("Demande spéciale", SPECIAL_REQUEST_FORM_HTML)


@app.post("/special-request/submit")
def special_request_submit():
    submission = {
        "employee_name": request.form.get("employee_name", ""),
        "product_reference": request.form.get("product_reference", ""),
        "max_retry_delay_value": request.form.get("max_retry_delay_value", ""),
        "engineering_third_salary_name": request.form.get("engineering_third_salary_name", ""),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if SPECIAL_REQUEST_FILE.exists():
        existing = json.loads(SPECIAL_REQUEST_FILE.read_text(encoding="utf-8"))
    existing.append(submission)
    SPECIAL_REQUEST_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return _page("Demande envoyée", "<p>Votre demande spéciale a bien été enregistrée.</p>")


@app.get("/contacts")
def contacts():
    """
    Benchmark v2, famille A (A3 — ambiguïté à résoudre,
    docs/briefs/B3-benchmark-v2.md). Karim Haddad et Chloé Simon
    apparaissent TOUS LES DEUX sous "Congés et absences" — ambiguïté
    délibérée, résolue seulement par le fixture docs
    (A3_DISAMBIGUATION_PAGE). Yann Morel (3e personne RH,
    hr_data.EMPLOYEES) n'est volontairement pas un candidat plausible ici
    (rôle sans rapport), pour ne pas diluer l'ambiguïté à 3 candidats.
    """
    rows = "\n".join(
        f"<tr><td>{name}</td><td>{role}</td><td>{email}</td></tr>"
        for name, role, email in [
            ("Karim Haddad", "Congés et absences", "karim.haddad@entreprise.fr"),
            ("Yann Morel", "Recrutement", "yann.morel@entreprise.fr"),
            ("Chloé Simon", "Congés et absences", "chloe.simon@entreprise.fr"),
        ]
    )
    body = f"""
<table>
<thead><tr><th>Nom</th><th>Rôle RH</th><th>Email</th></tr></thead>
<tbody>{rows}</tbody>
</table>
"""
    return _page("Contacts RH", body)


@app.get("/login")
def login_form():
    body = """
<form method="post" action="/login">
  <label>Identifiant : <input type="text" name="username" required></label><br>
  <label>Mot de passe : <input type="password" name="password" required></label><br>
  <button type="submit">Se connecter</button>
</form>
"""
    return _page("Connexion", body)


@app.post("/login")
def login_submit():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if username == hr_data.LOGIN_USERNAME and password == hr_data.LOGIN_PASSWORD:
        session.permanent = True
        session["logged_in"] = True
        session["csrf"] = secrets.token_hex(8)
        return redirect(url_for("leave_requests"))
    return _page("Connexion", "<p>Identifiants invalides.</p>"), 401


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_form"))


@app.get("/leave-requests")
def leave_requests():
    if not session.get("logged_in"):
        return redirect(url_for("login_form"))
    rows = "\n".join(
        f"<tr><td>{r['employee']}</td><td>{r['status']}</td></tr>" for r in hr_data.LEAVE_REQUESTS
    )
    pending = sum(1 for r in hr_data.LEAVE_REQUESTS if r["status"] == "en attente")
    body = f"""
<p>Demandes en attente : {pending}</p>
<table>
<thead><tr><th>Employé</th><th>Statut</th></tr></thead>
<tbody>{rows}</tbody>
</table>
"""
    return _page("Suivi des congés", body)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
