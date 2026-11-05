# bellhaven-crm-sync

Keeps the CRM's link between **Bellhaven Senior Living** and its facilities correct.
It scrapes the operator's website, matches each community to a CRM account, and queues
proposed fixes. A human then approves or rejects each one in a local review app.
**Nothing is written to the CRM without an approval.**

```
website ──scraper──┐
                   ├─ matcher (pure function) ─▶ proposals ─▶ SQLite queue ─▶ review app ─▶ CRM API
CRM API ──list─────┘                                          (local cache)     approve = write
                                                                    ▲                │
                                                                    └── state/decisions.jsonl (git)
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export CRM_TOKEN=<your token>
.venv/bin/python -m bellhaven.run      # scrape + match + queue (read-only against the CRM)
.venv/bin/python -m bellhaven.app      # review UI on http://localhost:5000
.venv/bin/python -m pytest -q tests    # unit tests (synthetic data, fake CRM)
```

| File | What it does |
|---|---|
| `bellhaven/scraper.py` | Crawls `/`, `/about` and every `/communities?page=N`, then each community page. Collects name, street, city, state, zip, care offerings, administrator, phone. |
| `bellhaven/normalize.py` | Street, zip, phone and name normalisation, plus mapping website care offerings to CRM `care_type`. |
| `bellhaven/matcher.py` | `build(locations, accounts) -> proposals, report`. A pure function with no I/O. |
| `bellhaven/store.py` | `state/decisions.jsonl` is the durable, git-tracked decision log. `state/sync.db` is a disposable local SQLite queue holding proposals, evidence and the run log. |
| `bellhaven/apply.py` | Runs an approved proposal: a stale-data check before each PATCH, and an idempotent POST. |
| `bellhaven/app.py` | Flask review app: evidence, the exact API calls, Approve/Reject, "Run pipeline now". |
| `.github/workflows/daily-sync.yml`, `crontab.example` | Daily schedule. |

## Matching approach

**A facility is identified by its physical address, not its name.** Names change with
every acquisition and rebrand ("Sunny Acres Retirement Home" became "Bellhaven Willow
Creek"). The address stays the same.

1. Normalise the street: `Boulevard→blvd`, `Road West→rd w`, `Pk→pike`, `Northwest→nw`, and so on. Normalise the city, the zip (first 5 digits) and the phone (digits only).
2. A CRM account is a **candidate** for a website location when the city and state match and **one** of these holds:
   - the normalised street matches. Zip is *not* required, which is how the wrong zip on Portsmouth gets caught and fixed.
   - the CRM street can't be used (a PO Box or blank). Then a phone match or a name similarity ≥ 0.85 is accepted, with "medium" confidence. This applies to Ashtabula.
3. A same-city account with a similar name but a **different street** is *not* a match. It is listed in the evidence as a "rejected lookalike" so the reviewer can see it was considered. Examples: "Amberly Manor" in Colorado Springs, and "Union Square Senior Living" at a different New Albany address.
4. **One account belongs to one location.** If two website locations match the same account (for example two wings
   listed separately at one address), the account goes to the location it matches best: street, then zip, then phone,
   then name. The other location loses it, and its proposal carries a warning plus a `shared_with_other_location`
   block in the evidence. No account ever gets conflicting renames or duplicate flags.
5. When several accounts share the address, one survivor is chosen in this order:
   - a reviewer's explicit choice;
   - already under Bellhaven;
   - Active;
   - **the website's administrator is a contact on the account** (this signal identifies 20 of the 35 facilities);
   - has revenue;
   - phone matches the website;
   - name similarity;
   - account id.

   If the top candidates are equal on every strong signal (name similarity and account id don't count), the
   proposal is flagged as a **tie**. The review app then shows a "Keep …" button for each tied account. A choice
   is saved to `decisions.jsonl` and the queue is rebuilt around it: the old proposals go stale and new ones appear.
   On this data only Kettering ties, because none of its three zero-revenue accounts has a contact.
6. The survivor is classified:
   - already under Bellhaven with matching fields → **confident match** (no proposal)
   - under Bellhaven with stale fields → **field fix**
   - under another parent or none → **CHOW** if `lifetime_revenue > 0 AND outstanding_ar > 0`, otherwise a **direct re-parent**
7. A Bellhaven-parented account that matches no website location:
   - if another operator's account exists at the same address, the facility was **sold**. With billing history: set `chow_current_account` on the old account to the new owner's *existing* account; nothing else changes. Without billing history: mark it a duplicate.
   - otherwise set `status = Needs Review` with a note. I don't change the parent, because the new owner can't be inferred from the data, and that call belongs to a person.

Phone numbers are only used as evidence and are never overwritten. The website phones and
CRM phones disagree for most facilities that are otherwise clearly the same, so I don't trust
either source enough to overwrite.

### CRM conventions used

| Situation | Write |
|---|---|
| Duplicate | `duplicate_of_account = <survivor>`, `status = Inactive`, plus an explanatory `note` |
| CHOW (revenue AND AR > 0) | POST a new account under Bellhaven, then PATCH **only** `chow_current_account` on the old account (no parent, name or status change) |
| Re-parent (otherwise) | PATCH `parent_id` (plus any name or address fixes) and a note recording the old parent |
| Not on website | `status = Needs Review` plus a note |
| New location | POST under Bellhaven, `status = Active`, with a note linking the source page |

Every note the tool writes starts with `[bellhaven-sync]`, so automated edits are easy to find.

## Safety and re-runs

- **Decisions are text in git, not a binary database.** Every approve, reject, apply or fail appends one JSON line
  to `state/decisions.jsonl` (last line per proposal id wins). `.gitattributes` marks it `merge=union`, so two
  reviewers, or a reviewer and CI, never get a merge conflict. The SQLite file is a cache: delete it and the next
  run rebuilds it, with decided items restored as history rather than re-proposed. Commit `decisions.jsonl` after a
  review session.
- **Read-only pipeline.** `bellhaven.run` never writes to the CRM. Only the Approve button does.
- **Fingerprinted proposals.** Each proposal's id is a hash of (kind, location, account, the exact changes).
  If a later run produces a fingerprint that is already queued or in `decisions.jsonl`, it is not proposed again,
  so approved or rejected items never come back, even on a fresh machine. If the underlying data changes, the fingerprint changes and a new proposal appears.
- **Stale proposals.** A pending proposal that the next run no longer generates is marked `stale`,
  so nobody approves it against outdated data.
- **Fixes take accounts out of play.** Accounts with `duplicate_of_account` or `chow_current_account` set
  are ignored by the matcher, so a completed CHOW or duplicate doesn't produce new proposals.
- **Conflict check.** Before each PATCH the account is read again. If a field we are about to change no
  longer holds the value the reviewer saw, the write is aborted and the proposal is marked `failed`.
- **Idempotent CHOW.** The id of the newly created account is saved straight after the POST, so retrying a
  half-applied CHOW never creates a second account.

The daily GitHub Action runs the tests and the read-only pipeline, reading `decisions.jsonl` so decided items stay
decided, and writes the pending queue to the job summary. It has `contents: read` permission and commits nothing,
so the reviewer is the only one who writes state. In production the decision log would live in a shared database,
but an append-only log is still the right shape for it.

## Writeup

**Time spent:** _fill in_

**How I used AI tools:** I directed Claude Code through the build. It probed the website and API,
dumped all 121 accounts and found the pattern: Harborview (acquired 2025) and Cedar Trail (2026)
facilities still sat under their old parents, plus duplicates at shared addresses, renamed facilities,
a PO-box address, a wrong zip, a sold facility (Sandusky, now under Millstone), and name-only lookalikes.
I reviewed each case by hand against the raw data before trusting the matcher. Then I pinned the rules
down with unit tests (CHOW branches, lookalike rejection, re-run idempotency, conflict abort). No LLM
calls are made at runtime; the matching is deterministic.

**Known limitations, deliberately left for later:**
- Approval is per proposal, not per field (e.g. you can't accept Kettering's re-parent but reject the rename).
- Sale detection needs an exact normalised street match with the buyer's account.
- Phone numbers are evidence only and are never updated.

**What I'd build next:**
- Store state in a shared DB and add reviewer identity and authentication to the review app.
- Use geocoding (USPS/Google) for addresses instead of a word map, to cover suites and unusual abbreviations.
- Use other signals for "not on website" facilities: state licensing databases or CMS provider data, to find the new owner automatically.
- Allow bulk approval of high-confidence, low-risk proposals (name fixes), with an audit log.
- Alert when website scrape counts drop sharply, since that usually means the site layout changed, not mass divestitures.
- Use the same pipeline config for other operators (parent id, site selectors).
