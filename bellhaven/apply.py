"""Execute an APPROVED proposal against the CRM. Only the review app calls this.

Safety:
* Before each PATCH the account is re-read and the fields we are about to
  change must still hold the values the reviewer saw ("expect"); otherwise the
  write is aborted as a conflict (someone else changed it).
* A created account id is persisted immediately after POST, so retrying a
  half-applied proposal never creates a second account.
"""
import json

from .store import load, log_decision, now


def resolve(value, created):
    if isinstance(value, str) and value.startswith("$"):
        return created[value[1:]]
    return value


def apply_proposal(con, crm, proposal_id):
    p = load(con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone())
    if p["status"] not in ("approved", "failed"):
        raise ValueError(f"proposal {proposal_id} is {p['status']}, not approved")
    created = p.get("created_ids") or {}
    log = []
    try:
        for op in p["ops"]:
            if op["op"] == "POST":
                if op["ref"] in created:
                    log.append(f"POST skipped, already created {created[op['ref']]}")
                    continue
                res = crm.create_account(op["body"])
                created[op["ref"]] = res["account_id"]
                con.execute("UPDATE proposals SET created_ids=? WHERE id=?", (json.dumps(created), proposal_id))
                con.commit()
                log_decision(con, proposal_id)  # created id survives even if the next step fails
                log.append(f"POST created {res['account_id']}")
            elif op["op"] == "PATCH":
                changes = {k: resolve(v, created) for k, v in op["changes"].items()}
                cur = crm.get_account(op["account_id"])
                if all(cur.get(k) == v for k, v in changes.items() if k != "note"):
                    log.append(f"PATCH {op['account_id']} already in desired state")
                    continue
                drift = {k: (v, cur.get(k)) for k, v in op.get("expect", {}).items() if cur.get(k) != v}
                if drift:
                    raise RuntimeError(f"conflict on {op['account_id']}: CRM changed since proposal {drift}")
                crm.update_account(op["account_id"], changes)
                log.append(f"PATCH {op['account_id']} {sorted(changes)}")
        status = "applied"
    except Exception as e:  # keep going for other proposals; reviewer sees the error
        log.append(f"ERROR: {e}")
        status = "failed"
    con.execute("UPDATE proposals SET status=?, result=? WHERE id=?",
                (status, json.dumps({"at": now(), "log": log}), proposal_id))
    con.commit()
    log_decision(con, proposal_id)
    return status, log
