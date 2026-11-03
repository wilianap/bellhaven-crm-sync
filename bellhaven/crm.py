"""Thin client for the CRM sandbox API."""
import os
import requests

BASE = os.environ.get("CRM_BASE", "https://analyst-assessment-production.up.railway.app")
API = BASE + "/api/v1"


def token():
    t = os.environ.get("CRM_TOKEN")
    if not t:
        raise SystemExit("Set CRM_TOKEN (see README).")
    return t


def crm_browser_url(account_id):
    return f"{BASE}/crm/{token()}/accounts/{account_id}"


class CRM:
    def __init__(self, tok=None):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {tok or token()}"

    def _req(self, method, path, **kw):
        r = self.s.request(method, API + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def list_accounts(self, **params):
        out, page = [], 1
        while True:
            d = self._req("GET", "/accounts", params={**params, "page": page, "page_size": 100})
            out += d["data"]
            if len(out) >= d["total"] or not d["data"]:
                return out
            page += 1

    def get_account(self, account_id):
        return self._req("GET", f"/accounts/{account_id}")

    def create_account(self, body):
        return self._req("POST", "/accounts", json=body)

    def update_account(self, account_id, changes):
        return self._req("PATCH", f"/accounts/{account_id}", json=changes)
