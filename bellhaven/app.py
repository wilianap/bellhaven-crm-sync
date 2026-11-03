"""Local review app. Nothing is written to the CRM until a reviewer clicks Approve.

Run:  CRM_TOKEN=... python -m bellhaven.app   ->  http://localhost:5000
"""
import json
import os

from flask import Flask, abort, redirect, render_template, request, url_for

from .apply import apply_proposal
from .crm import CRM, crm_browser_url
from .normalize import care_types, norm_phone, norm_street, norm_zip
from .store import connect, load, log_decision, now

app = Flask(__name__)
STATUSES = ["pending", "applied", "rejected", "failed", "stale", "approved"]
KIND_ORDER = ["chow", "chow_link", "reparent", "duplicate", "update", "new_account", "not_on_website"]
KIND_LABEL = {
    "chow": "CHOW (new account)", "chow_link": "CHOW link", "reparent": "Re-parent",
    "duplicate": "Duplicate", "update": "Field fix", "new_account": "New account",
    "not_on_website": "Not on website",
}


@app.template_filter("pretty")
def pretty(v):
    return json.dumps(v, indent=2)


def differs(field, web, crm):
    """Highlight only differences the matcher would act on (same normalisation)."""
    if field == "street":
        return norm_street(web) != norm_street(crm or "")
    if field == "phone":
        return norm_phone(web) != norm_phone(crm or "")
    if field == "zip":
        return norm_zip(web) != norm_zip(crm or "")
    if field == "care_offerings":
        return (crm or "") not in care_types(web)
    return str(web).strip().lower() != str(crm or "").strip().lower()


@app.context_processor
def helpers():
    return {"crm_url": crm_browser_url, "KIND_LABEL": KIND_LABEL, "differs": differs}


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    kind = request.args.get("kind", "")
    con = connect()
    q, args = "SELECT * FROM proposals WHERE 1=1", []
    if status != "all":
        q += " AND status=?"; args.append(status)
    if kind:
        q += " AND kind=?"; args.append(kind)
    rows = [load(r) for r in con.execute(q, args)]
    rows.sort(key=lambda p: (KIND_ORDER.index(p["kind"]) if p["kind"] in KIND_ORDER else 99, p["title"]))
    counts = dict(con.execute("SELECT status, COUNT(*) FROM proposals GROUP BY status").fetchall())
    kinds = dict(con.execute("SELECT kind, COUNT(*) FROM proposals WHERE status=? GROUP BY kind",
                             (status,)).fetchall()) if status != "all" else {}
    run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    report = json.loads(run["report"]) if run and run["report"] else {"matched": []}
    return render_template("index.html", rows=rows, status=status, kind=kind, counts=counts,
                           kinds=kinds, statuses=STATUSES, run=run, report=report)


@app.post("/proposals/<pid>/approve")
def approve(pid):
    con = connect()
    row = con.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone() or abort(404)
    if row["status"] not in ("pending", "failed", "approved"):  # approved = crashed before applying
        abort(409)
    con.execute("UPDATE proposals SET status='approved', decided_at=?, reviewer_comment=? WHERE id=?",
                (now(), request.form.get("comment", ""), pid))
    con.commit()
    log_decision(con, pid)  # durable record of the approval before any CRM write
    apply_proposal(con, CRM(), pid)
    return redirect(request.referrer or url_for("index"))


@app.post("/proposals/<pid>/reject")
def reject(pid):
    con = connect()
    row = con.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone() or abort(404)
    if row["status"] != "pending":
        abort(409)
    con.execute("UPDATE proposals SET status='rejected', decided_at=?, reviewer_comment=? WHERE id=?",
                (now(), request.form.get("comment", ""), pid))
    con.commit()
    log_decision(con, pid)
    return redirect(request.referrer or url_for("index"))


@app.post("/run")
def run_pipeline():
    from .run import main
    main()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
