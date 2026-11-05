"""Match scraped website locations to CRM accounts and generate proposals.

Pure functions: (locations, accounts) -> (proposals, report). No I/O, so the
logic is unit-testable and the pipeline can be re-run safely.

Identity of a facility = its physical address (street + city + state).
Names change with every rebrand/acquisition, so a name match alone is never
enough; a different street address in the same city is treated as a different
facility. Name/phone are only used as a fallback when the CRM address is
unusable (e.g. a PO Box billing address).
"""
import hashlib
import json

from .normalize import (care_types, is_po_box, name_similarity, norm_city, norm_phone,
                        norm_street, norm_zip)

PARENT_NAME = "Bellhaven Senior Living (Parent Account)"
TAG = "[bellhaven-sync]"


def has_billing_history(acc):
    """SOP: billing must keep the old account if it has revenue AND open AR."""
    return (acc.get("lifetime_revenue") or 0) > 0 and (acc.get("outstanding_ar") or 0) > 0


def is_superseded(acc):
    """Accounts already resolved as a duplicate or by a CHOW are out of play."""
    return bool(acc.get("duplicate_of_account") or acc.get("chow_current_account"))


def append_note(acc, text):
    cur = (acc.get("note") or "").strip()
    if text in cur:
        return cur
    return f"{cur}\n{text}".strip()


def fingerprint(kind, slug, account_id, ops):
    """Stable id for a proposal. Notes are excluded so wording tweaks don't re-propose."""
    def strip(o):
        o = json.loads(json.dumps(o))
        for k in ("changes", "body"):
            if k in o:
                o[k].pop("note", None)
        o.pop("expect", None)
        return o
    raw = json.dumps([kind, slug, account_id, [strip(o) for o in ops]], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- signals

def signals(loc, acc):
    same_city = (norm_city(loc["city"]) == norm_city(acc["billing_city"])
                 and loc["state"] == acc["billing_state"])
    usable = not is_po_box(acc["billing_street"]) and bool(acc["billing_street"].strip())
    street = usable and norm_street(loc["street"]) == norm_street(acc["billing_street"])
    return {
        "same_city": same_city,
        "street_match": street,
        "zip_match": norm_zip(loc["zip"]) == norm_zip(acc["billing_zip"]),
        "crm_address_usable": usable,
        "phone_match": bool(norm_phone(loc["phone"])) and norm_phone(loc["phone"]) == norm_phone(acc["phone"]),
        "name_similarity": name_similarity(loc["name"], acc["name"]),
        # Website administrator is a contact on this account (accounts carry `contact_names`
        # when run.py fetched contacts; absent in tests -> False).
        "admin_is_contact": bool(loc.get("administrator")) and
            norm_city(loc["administrator"]) in {norm_city(n) for n in acc.get("contact_names", [])},
    }


def is_candidate(sig):
    if not sig["same_city"]:
        return False
    if sig["street_match"]:
        return True
    # Fallback only when the CRM address can't be compared (PO Box / blank).
    if not sig["crm_address_usable"]:
        return sig["phone_match"] or sig["name_similarity"] >= 0.85
    return False


def confidence(sig):
    if sig["street_match"] and sig["zip_match"]:
        return "high"
    if sig["street_match"]:
        return "high (zip differs)"
    return "medium (address unusable; matched on " + (
        "phone + name" if sig["phone_match"] and sig["name_similarity"] >= 0.85
        else "phone" if sig["phone_match"] else "name") + ")"


# ---------------------------------------------------------------- helpers

def acc_view(acc, parents):
    keys = ["account_id", "name", "parent_id", "billing_street", "billing_city", "billing_state",
            "billing_zip", "care_type", "status", "phone", "lifetime_revenue", "outstanding_ar",
            "chow_current_account", "duplicate_of_account", "note"]
    v = {k: acc.get(k) for k in keys}
    v["parent_name"] = parents.get(acc.get("parent_id"), {}).get("name", "(none)") if acc.get("parent_id") else "(none)"
    return v


def field_diffs(loc, acc):
    """Website-vs-CRM field fixes for a matched account (phone deliberately excluded)."""
    want = {}
    if loc["name"].strip() != acc["name"].strip():
        want["name"] = loc["name"].strip()
    if norm_street(loc["street"]) != norm_street(acc["billing_street"]):
        want["billing_street"] = loc["street"]
    if norm_city(loc["city"]) != norm_city(acc["billing_city"]):
        want["billing_city"] = loc["city"]
    if loc["state"] != acc["billing_state"]:
        want["billing_state"] = loc["state"]
    if norm_zip(loc["zip"]) != norm_zip(acc["billing_zip"]):
        want["billing_zip"] = loc["zip"]
    cts = care_types(loc["care_offerings"])
    if cts and acc.get("care_type") not in cts:
        want["care_type"] = cts[0]
    if acc.get("status") != "Active":
        want["status"] = "Active"
    return want


def patch(acc, changes):
    return {"op": "PATCH", "account_id": acc["account_id"], "changes": changes,
            "expect": {k: acc.get(k) for k in changes if k != "note"}}


def claim_score(sig):
    """How strongly a location claims an account (used when two locations want the same one)."""
    return (sig["street_match"], sig["zip_match"], sig["phone_match"], sig["name_similarity"])


def survivor_rank(bh_id, override=None):
    """Sort key for picking the surviving account among same-address candidates.

    A reviewer's explicit choice wins; then evidence of which record is the live one.
    The account id is only a last resort, and when it decides, the proposal is flagged as a tie.
    """
    def key(c):
        acc, sig = c
        return (acc["account_id"] == override,
                acc["parent_id"] == bh_id, acc.get("status") == "Active",
                sig.get("admin_is_contact", False),
                (acc.get("lifetime_revenue") or 0), sig["phone_match"],
                sig["name_similarity"], acc["account_id"])
    return key


def tie_info(cands, rank):
    """Account ids tied with the top candidate on every STRONG signal, or None.

    Ignores the reviewer override (so the choice stays visible after picking) and
    name similarity (old-owner names say little about which record should survive).
    """
    if len(cands) < 2:
        return None
    strong = lambda c: rank(c)[1:-2]
    tied = [a["account_id"] for a, s in cands if strong((a, s)) == strong(cands[0])]
    return tied if len(tied) > 1 else None


# ---------------------------------------------------------------- main

def build(locations, accounts, survivor_overrides=None):
    survivor_overrides = survivor_overrides or {}
    parents = {a["account_id"]: a for a in accounts if a["name"].endswith("(Parent Account)")}
    bh = next(a for a in accounts if a["name"] == PARENT_NAME)
    bh_id = bh["account_id"]
    facilities = [a for a in accounts if a["account_id"] not in parents]
    live = [a for a in facilities if not is_superseded(a)]

    proposals, report = [], {"matched": [], "new": [], "not_on_website": []}
    claimed = set()

    def add(kind, loc, acc, title, summary, ops, evidence):
        slug = loc["slug"] if loc else None
        aid = acc["account_id"] if acc else None
        proposals.append({
            "id": fingerprint(kind, slug, aid, ops), "kind": kind, "title": title,
            "summary": summary, "location_slug": slug, "account_id": aid,
            "ops": ops, "evidence": evidence,
        })

    # Pass 1: candidates per location.
    per_loc = []
    for loc in locations:
        sigs = [(a, signals(loc, a)) for a in live]
        per_loc.append((sigs, [c for c in sigs if is_candidate(c[1])]))

    # Each account may belong to ONE location. If several locations claim it
    # (e.g. two wings listed separately at one address), it goes to the location
    # it matches best; the others lose it and say so in their evidence.
    owner = {}
    for i, (_, cands) in enumerate(per_loc):
        for a, s in cands:
            score = claim_score(s)
            if a["account_id"] not in owner or score > owner[a["account_id"]][0]:
                owner[a["account_id"]] = (score, i)

    for i, loc in enumerate(locations):
        sigs, all_cands = per_loc[i]
        override = survivor_overrides.get(loc["slug"])
        rank = survivor_rank(bh_id, override)
        cands = sorted([c for c in all_cands if owner[c[0]["account_id"]][1] == i], key=rank, reverse=True)
        tied = tie_info(cands, rank)
        shared = [{"account": acc_view(a, parents), "signals": s,
                   "assigned_to": locations[owner[a["account_id"]][1]]["slug"]}
                  for a, s in all_cands if owner[a["account_id"]][1] != i]
        lookalikes = [
            {"account": acc_view(a, parents), "signals": s,
             "why_not": "different city/state" if not s["same_city"] else "different street address"}
            for a, s in sigs if not is_candidate(s) and s["name_similarity"] >= 0.6
        ]
        evidence_base = {"website": loc, "rejected_lookalikes": lookalikes,
                         "candidates": [{"account": acc_view(a, parents), "signals": s} for a, s in cands]}
        if shared:
            evidence_base["shared_with_other_location"] = shared

        if not cands:
            cts = care_types(loc["care_offerings"])
            body = {"name": loc["name"], "parent_id": bh_id, "billing_street": loc["street"],
                    "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"],
                    "care_type": cts[0] if cts else "", "phone": loc["phone"], "status": "Active",
                    "note": f"{TAG} Created from Bellhaven website listing {loc['url']}"}
            summary = f"No CRM account at {loc['street']}, {loc['city']} {loc['state']}. Create it under Bellhaven."
            if shared:
                summary = (f"WARNING: the CRM account(s) at this address match another website location better "
                           f"({', '.join(sorted({x['assigned_to'] for x in shared}))}). Approve only if these really are "
                           f"separate facilities; otherwise reject. ") + summary
            add("new_account", loc, None, f"Create account: {loc['name']}", summary,
                [{"op": "POST", "ref": "new", "body": body}], evidence_base)
            report["new"].append(loc["slug"])
            continue

        surv, ssig = cands[0]
        claimed.update(a["account_id"] for a, _ in cands)
        if tied:
            evidence_base["tie"] = tied
        if override and override == surv["account_id"]:
            evidence_base["survivor_chosen_by_reviewer"] = True
        ev = {**evidence_base, "survivor": surv["account_id"], "confidence": confidence(ssig)}
        diffs = field_diffs(loc, surv)
        old_parent = parents.get(surv["parent_id"], {}).get("name", "(no parent)")

        if surv["parent_id"] != bh_id and has_billing_history(surv):
            # SOP CHOW: keep old account untouched, create a new one, link old -> new.
            cts = care_types(loc["care_offerings"])
            body = {"name": loc["name"], "parent_id": bh_id, "billing_street": loc["street"],
                    "billing_city": loc["city"], "billing_state": loc["state"], "billing_zip": loc["zip"],
                    "care_type": cts[0] if cts else surv["care_type"], "phone": loc["phone"], "status": "Active",
                    "note": f"{TAG} CHOW: new Bellhaven account; previous owner record {surv['account_id']} "
                            f"({old_parent}) kept for billing."}
            ops = [{"op": "POST", "ref": "new", "body": body},
                   {"op": "PATCH", "account_id": surv["account_id"], "changes": {"chow_current_account": "$new"},
                    "expect": {"chow_current_account": surv.get("chow_current_account", "")}}]
            add("chow", loc, surv, f"CHOW: {surv['name']} ({old_parent}) -> Bellhaven",
                f"Facility now Bellhaven but the old account has lifetime revenue ${surv['lifetime_revenue']:,} and "
                f"outstanding AR ${surv['outstanding_ar']:,}. Per SOP: leave it as-is, create a new account under "
                f"Bellhaven, set chow_current_account on the old one.", ops, ev)
        elif surv["parent_id"] != bh_id:
            changes = {"parent_id": bh_id, **diffs,
                       "note": append_note(surv, f"{TAG} Re-parented from {old_parent} to Bellhaven "
                                                 f"(listed on Bellhaven website as '{loc['name']}').")}
            why = ("no revenue history" if not surv.get("lifetime_revenue") else "no outstanding AR")
            add("reparent", loc, surv, f"Re-parent: {surv['name']} ({old_parent}) -> Bellhaven",
                f"Website lists this facility as Bellhaven; CRM has it under {old_parent}. "
                f"SOP allows direct re-parent ({why})." + (f" Also fixes: {', '.join(k for k in diffs)}." if diffs else ""),
                [patch(surv, changes)], ev)
        elif diffs:
            add("update", loc, surv, f"Fix fields: {surv['name']}",
                "Correct parent; fields differ from website: " + ", ".join(
                    f"{k}: '{surv.get(k)}' -> '{v}'" for k, v in diffs.items()),
                [patch(surv, diffs)], ev)
        else:
            report["matched"].append({"slug": loc["slug"], "account_id": surv["account_id"],
                                      "name": surv["name"], "confidence": confidence(ssig)})

        for dup, dsig in cands[1:]:
            dparent = parents.get(dup["parent_id"], {}).get("name", "(no parent)")
            dev = {**evidence_base, "survivor": surv["account_id"], "duplicate": dup["account_id"]}
            if dup["parent_id"] != bh_id and has_billing_history(dup):
                add("chow_link", loc, dup, f"CHOW link: {dup['name']} ({dparent}) -> {surv['account_id']}",
                    "Same facility as the surviving Bellhaven account, but this old-owner record carries "
                    "revenue + open AR, so it is kept and linked via chow_current_account instead of being "
                    "marked a duplicate.",
                    [{"op": "PATCH", "account_id": dup["account_id"],
                      "changes": {"chow_current_account": surv["account_id"]},
                      "expect": {"chow_current_account": dup.get("chow_current_account", "")}}], dev)
                continue
            changes = {"duplicate_of_account": surv["account_id"], "status": "Inactive",
                       "note": append_note(dup, f"{TAG} Duplicate of {surv['account_id']} ('{loc['name']}'): "
                                                f"same address {loc['street']}, {loc['city']} {loc['state']}.")}
            add("duplicate", loc, dup, f"Duplicate: {dup['name']} ({dparent}) -> {surv['name']}",
                f"Same physical address as the surviving account {surv['account_id']}. Mark as duplicate and Inactive.",
                [patch(dup, changes)], dev)

    # Bellhaven accounts that the website no longer lists.
    for acc in live:
        if acc["parent_id"] != bh_id or acc["account_id"] in claimed or acc.get("status") == "Inactive":
            continue
        others = [o for o in live if o["parent_id"] not in ("", bh_id) and o["account_id"] != acc["account_id"]
                  and norm_city(o["billing_city"]) == norm_city(acc["billing_city"])
                  and o["billing_state"] == acc["billing_state"]
                  and norm_street(o["billing_street"]) == norm_street(acc["billing_street"])]
        ev = {"crm_account": acc_view(acc, parents), "website_has_it": False,
              "same_address_other_operator": [acc_view(o, parents) for o in others]}
        report["not_on_website"].append(acc["account_id"])
        if others:
            new = others[0]
            nparent = parents.get(new["parent_id"], {}).get("name", "?")
            if has_billing_history(acc):
                add("chow_link", None, acc, f"Sold: {acc['name']} -> {nparent}",
                    f"Not on Bellhaven's website; same address is in the CRM under {nparent} "
                    f"({new['account_id']}). Old account has revenue ${acc['lifetime_revenue']:,} and AR "
                    f"${acc['outstanding_ar']:,}, so per SOP it stays as-is and gets "
                    f"chow_current_account = {new['account_id']} (the new owner's existing account).",
                    [{"op": "PATCH", "account_id": acc["account_id"],
                      "changes": {"chow_current_account": new["account_id"]},
                      "expect": {"chow_current_account": acc.get("chow_current_account", "")}}], ev)
            else:
                changes = {"duplicate_of_account": new["account_id"], "status": "Inactive",
                           "note": append_note(acc, f"{TAG} Facility now owned by {nparent}; duplicate of "
                                                    f"{new['account_id']}.")}
                add("duplicate", None, acc, f"Sold: {acc['name']} -> {nparent} (duplicate)",
                    f"Not on Bellhaven's website; the new owner's account {new['account_id']} already exists. "
                    "No billing history, so mark this one duplicate + Inactive.", [patch(acc, changes)], ev)
        elif acc.get("status") != "Needs Review":
            changes = {"status": "Needs Review",
                       "note": append_note(acc, f"{TAG} No longer listed on the Bellhaven website; "
                                                "confirm whether divested/closed and who the new owner is.")}
            add("not_on_website", None, acc, f"Not on website: {acc['name']}",
                "Under the Bellhaven parent but not listed on the website, and no other operator's account "
                "exists at this address. Flag for human follow-up (Needs Review) rather than guessing a new owner.",
                [patch(acc, changes)], ev)

    return proposals, report
