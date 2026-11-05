"""Daily pipeline: scrape -> fetch CRM -> match -> store proposals. Never writes to the CRM."""
import json

from .crm import CRM
from .matcher import build
from .scraper import scrape
from .store import connect, now, read_survivor_overrides, record_run


def main():
    started = now()
    locations = scrape()
    crm = CRM()
    accounts = crm.list_accounts()
    names = {}
    for c in crm.list_contacts():
        if c.get("is_active", True):
            names.setdefault(c["account_id"], []).append(c["name"])
    for a in accounts:
        a["contact_names"] = names.get(a["account_id"], [])
    con = connect()
    proposals, report = build(locations, accounts, read_survivor_overrides(con.decisions_path))
    run_id, new, stale = record_run(con, locations, accounts, proposals, report, started)
    pending = con.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
    print(json.dumps({"run": run_id, "locations": len(locations), "accounts": len(accounts),
                      "proposals_generated": len(proposals), "new_proposals": new,
                      "marked_stale": stale, "pending_total": pending,
                      "confident_matches": len(report["matched"])}, indent=1))


if __name__ == "__main__":
    main()
