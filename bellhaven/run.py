"""Daily pipeline: scrape -> fetch CRM -> match -> store proposals. Never writes to the CRM."""
import json

from .crm import CRM
from .matcher import build
from .scraper import scrape
from .store import connect, now, record_run


def main():
    started = now()
    locations = scrape()
    accounts = CRM().list_accounts()
    proposals, report = build(locations, accounts)
    con = connect()
    run_id, new, stale = record_run(con, locations, accounts, proposals, report, started)
    pending = con.execute("SELECT COUNT(*) FROM proposals WHERE status='pending'").fetchone()[0]
    print(json.dumps({"run": run_id, "locations": len(locations), "accounts": len(accounts),
                      "proposals_generated": len(proposals), "new_proposals": new,
                      "marked_stale": stale, "pending_total": pending,
                      "confident_matches": len(report["matched"])}, indent=1))


if __name__ == "__main__":
    main()
