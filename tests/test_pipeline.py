import json

import pytest

from bellhaven.apply import apply_proposal
from bellhaven.matcher import build
from bellhaven.normalize import norm_street
from bellhaven.store import connect, record_run

BH, OLD, OTHER = "P_BH", "P_OLD", "P_OTHER"


def acc(aid, name, street, city="Springfield", state="OH", zip_="45503", parent=BH, rev=0, ar=0, **kw):
    a = {"account_id": aid, "name": name, "parent_id": parent, "billing_street": street,
         "billing_city": city, "billing_state": state, "billing_zip": zip_, "care_type": "Assisted Living",
         "status": "Active", "phone": "(555) 000-0000", "lifetime_revenue": rev, "outstanding_ar": ar,
         "chow_current_account": "", "duplicate_of_account": "", "note": ""}
    a.update(kw)
    return a


def loc(slug, name, street, city="Springfield", state="OH", zip_="45503", phone="(555) 111-1111"):
    return {"slug": slug, "url": f"https://x/{slug}", "name": name, "street": street, "city": city,
            "state": state, "zip": zip_, "care_offerings": ["Assisted Living"], "administrator": "", "phone": phone}


PARENTS = [acc(BH, "Bellhaven Senior Living (Parent Account)", "", parent=""),
           acc(OLD, "Old Owner (Parent Account)", "", parent=""),
           acc(OTHER, "Other Co (Parent Account)", "", parent="")]


def kinds(props):
    return sorted(p["kind"] for p in props)


def test_street_normalisation():
    assert norm_street("199 Barks Road West") == norm_street("199 Barks Rd W")
    assert norm_street("3313 Wilmington Pk") == norm_street("3313 Wilmington Pike")


def test_confident_match_needs_nothing():
    props, rep = build([loc("a", "Bellhaven A", "1 Main Street")], PARENTS + [acc("1", "Bellhaven A", "1 Main St")])
    assert props == [] and rep["matched"][0]["account_id"] == "1"


def test_chow_when_revenue_and_ar():
    props, _ = build([loc("a", "Bellhaven A", "1 Main St")],
                     PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD, rev=10, ar=5)])
    (p,) = props
    assert p["kind"] == "chow"
    post, link = p["ops"]
    assert post["op"] == "POST" and post["body"]["parent_id"] == BH
    # old account: ONLY chow_current_account is touched
    assert link == {"op": "PATCH", "account_id": "1", "changes": {"chow_current_account": "$new"},
                    "expect": {"chow_current_account": ""}}


@pytest.mark.parametrize("rev,ar", [(0, 0), (10, 0), (0, 5)])
def test_direct_reparent_otherwise(rev, ar):
    props, _ = build([loc("a", "Bellhaven A", "1 Main St")],
                     PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD, rev=rev, ar=ar)])
    (p,) = props
    assert p["kind"] == "reparent"
    assert p["ops"][0]["changes"]["parent_id"] == BH and p["ops"][0]["changes"]["name"] == "Bellhaven A"


def test_duplicates_keep_bellhaven_copy():
    props, _ = build([loc("a", "Bellhaven A", "1 Main St")],
                     PARENTS + [acc("1", "Old A", "1 Main Street", parent=OLD), acc("2", "Bellhaven A", "1 Main St")])
    (p,) = props
    assert p["kind"] == "duplicate" and p["account_id"] == "1"
    assert p["ops"][0]["changes"]["duplicate_of_account"] == "2"
    assert p["ops"][0]["changes"]["status"] == "Inactive"


def test_name_only_lookalike_is_not_a_match():
    # Same name, different city -> new account; same city but different street -> new account
    props, _ = build([loc("a", "Amberly Manor", "4 Darrow Rd", city="Hudson")],
                     PARENTS + [acc("1", "Amberly Manor", "9 Nevada Ave", city="Colorado Springs", state="CO", parent=OTHER),
                                acc("2", "Amberly Manor Senior Living", "77 Other Rd", city="Hudson", parent=OTHER)])
    (p,) = props
    assert p["kind"] == "new_account"
    assert {l["account"]["account_id"] for l in p["evidence"]["rejected_lookalikes"]} == {"1", "2"}


def test_po_box_falls_back_to_phone():
    props, _ = build([loc("a", "Bellhaven A", "3 Real Rd", phone="(555) 222-3333")],
                     PARENTS + [acc("1", "Bellhaven A", "PO Box 5", phone="555.222.3333")])
    (p,) = props
    assert p["kind"] == "update" and p["ops"][0]["changes"] == {"billing_street": "3 Real Rd"}


def test_not_on_website_and_sold():
    accounts = PARENTS + [acc("1", "Bellhaven Gone", "5 Gone Rd"),
                          acc("2", "Bellhaven Sold", "6 Sold Rd", rev=10, ar=5),
                          acc("3", "NewCo Sold", "6 Sold Road", parent=OTHER)]
    props, _ = build([], accounts)
    by = {p["account_id"]: p for p in props}
    assert by["1"]["kind"] == "not_on_website" and by["1"]["ops"][0]["changes"]["status"] == "Needs Review"
    assert by["2"]["kind"] == "chow_link" and by["2"]["ops"][0]["changes"] == {"chow_current_account": "3"}


def test_superseded_accounts_ignored_on_rerun():
    # after a CHOW was applied: old account has chow_current_account, new account exists under BH
    accounts = PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD, rev=10, ar=5, chow_current_account="9"),
                          acc("9", "Bellhaven A", "1 Main St")]
    props, rep = build([loc("a", "Bellhaven A", "1 Main St")], accounts)
    assert props == [] and rep["matched"][0]["account_id"] == "9"


def test_rerun_does_not_repropose_decided(tmp_path):
    con = connect(str(tmp_path / "s.db"))
    locs = [loc("a", "Bellhaven A", "1 Main St")]
    accs = PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD)]
    props, rep = build(locs, accs)
    record_run(con, locs, accs, props, rep, "t0")
    con.execute("UPDATE proposals SET status='rejected'")
    con.commit()
    _, new, stale = record_run(con, locs, accs, *build(locs, accs), "t1")
    assert new == 0 and stale == 0
    assert con.execute("SELECT COUNT(*), MAX(status) FROM proposals").fetchone()[:] == (1, "rejected")


class FakeCRM:
    def __init__(self, accounts):
        self.db = {a["account_id"]: dict(a) for a in accounts}
        self.posts = 0

    def get_account(self, aid):
        return dict(self.db[aid])

    def update_account(self, aid, ch):
        self.db[aid].update(ch)

    def create_account(self, body):
        self.posts += 1
        aid = f"NEW{self.posts}"
        self.db[aid] = {"account_id": aid, **body}
        return self.db[aid]


def _approve_all(con, crm):
    out = []
    for (pid,) in con.execute("SELECT id FROM proposals").fetchall():
        con.execute("UPDATE proposals SET status='approved' WHERE id=?", (pid,))
        out.append(apply_proposal(con, crm, pid))
    return out


def test_apply_chow_end_to_end_and_idempotent(tmp_path):
    con = connect(str(tmp_path / "s.db"))
    locs = [loc("a", "Bellhaven A", "1 Main St")]
    accs = PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD, rev=10, ar=5)]
    record_run(con, locs, accs, *build(locs, accs), "t0")
    crm = FakeCRM(accs)
    [(status, _)] = _approve_all(con, crm)
    assert status == "applied" and crm.posts == 1
    assert crm.db["1"]["parent_id"] == OLD and crm.db["1"]["chow_current_account"] == "NEW1"
    assert crm.db["NEW1"]["parent_id"] == BH
    # retrying never creates a second account
    con.execute("UPDATE proposals SET status='failed'")
    apply_proposal(con, crm, con.execute("SELECT id FROM proposals").fetchone()[0])
    assert crm.posts == 1
    # next run against the corrected CRM: nothing to propose
    props, rep = build(locs, list(crm.db.values()))
    assert props == []


def test_apply_aborts_on_conflict(tmp_path):
    con = connect(str(tmp_path / "s.db"))
    locs = [loc("a", "Bellhaven A", "1 Main St")]
    accs = PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD)]
    record_run(con, locs, accs, *build(locs, accs), "t0")
    crm = FakeCRM(accs)
    crm.db["1"]["parent_id"] = OTHER  # someone changed it after the proposal was made
    [(status, log)] = _approve_all(con, crm)
    assert status == "failed" and "conflict" in log[-1]
    assert crm.db["1"]["parent_id"] == OTHER


def test_one_account_never_claimed_by_two_locations():
    # Two website listings at one address, one CRM account: the better match keeps it.
    locs = [loc("a", "Bellhaven A Memory Care", "1 Main St", phone="(555) 999-0000"),
            loc("b", "Bellhaven A", "1 Main St", phone="(555) 123-4567")]
    accs = PARENTS + [acc("1", "Bellhaven A", "1 Main St", phone="(555) 123-4567")]
    props, rep = build(locs, accs)
    assert [m["slug"] for m in rep["matched"]] == ["b"]
    touching_1 = [p for p in props if p["account_id"] == "1"]
    assert touching_1 == []  # no conflicting rename / duplicate on the shared account
    (p,) = props
    assert p["kind"] == "new_account" and p["location_slug"] == "a"
    assert p["summary"].startswith("WARNING")
    assert p["evidence"]["shared_with_other_location"][0]["assigned_to"] == "b"


def test_decisions_survive_a_fresh_cache(tmp_path):
    """Decisions live in decisions.jsonl (git); the SQLite cache can be thrown away."""
    from bellhaven.store import log_decision, read_decisions
    dec = str(tmp_path / "decisions.jsonl")
    locs = [loc("a", "Bellhaven A", "1 Main St")]
    accs = PARENTS + [acc("1", "Old A", "1 Main St", parent=OLD), acc("2", "Bellhaven Gone", "9 Gone Rd")]

    a = connect(str(tmp_path / "machine_a.db"), dec)
    record_run(a, locs, accs, *build(locs, accs), "t0")
    reparent, gone = sorted(r[0] for r in a.execute("SELECT id FROM proposals"))
    a.execute("UPDATE proposals SET status='rejected' WHERE id=?", (reparent,))
    a.commit()
    log_decision(a, reparent)

    # Machine B (or CI) starts from an empty cache but the same decisions file.
    b = connect(str(tmp_path / "machine_b.db"), dec)
    _, new, _ = record_run(b, locs, accs, *build(locs, accs), "t1")
    rows = dict(b.execute("SELECT id, status FROM proposals").fetchall())
    assert rows[reparent] == "rejected"          # not re-proposed
    assert rows[gone] == "pending" and new == 1  # undecided work still shows up

    # A later decision on B flows back to A's existing cache on next connect.
    b.execute("UPDATE proposals SET status='rejected' WHERE id=?", (gone,))
    b.commit()
    log_decision(b, gone)
    a2 = connect(str(tmp_path / "machine_a.db"), dec)
    assert a2.execute("SELECT status FROM proposals WHERE id=?", (gone,)).fetchone()[0] == "rejected"
    assert len(read_decisions(dec)) == 2


def test_decision_log_last_line_wins_after_union_merge(tmp_path):
    from bellhaven.store import read_decisions
    p = tmp_path / "decisions.jsonl"
    p.write_text("\n".join(json.dumps(d) for d in [
        {"id": "x", "status": "approved"}, {"id": "y", "status": "rejected"}, {"id": "x", "status": "applied"}]) + "\n")
    assert {k: v["status"] for k, v in read_decisions(str(p)).items()} == {"x": "applied", "y": "rejected"}
