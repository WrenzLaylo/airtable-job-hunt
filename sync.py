"""Sync a job-application tracker (JSON) into an Airtable base.

Creates three linked tables if they don't exist yet (Companies, Applications,
Events), then upserts every company, application and dated event, so running
it twice changes nothing the second time.

    set AIRTABLE_TOKEN=<personal access token>   (scopes: data.records:read/write,
                                                   schema.bases:read/write)
    python sync.py --base appXXXXXXXXXXXXXX applications.json [--dry-run]

Airtable allows 5 requests per second per base and answers 429 when that is
exceeded; the client paces its calls and backs off for 30 s on a 429, as the
API docs ask. 5xx responses are retried with exponential backoff.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import OrderedDict

import requests

API = "https://api.airtable.com/v0"
BATCH = 10  # Airtable's max records per create/update call
STATUSES = ["Offer", "Interview", "In process", "Applied", "Unconfirmed", "Rejected"]
MODES = ["Remote", "Hybrid", "Onsite", "Not stated"]


def choices(names):
    return {"choices": [{"name": n} for n in names]}


# Table schema. The first field is the table's primary field. Links are added
# after the tables exist, because a link field needs the other table's id.
SCHEMA = OrderedDict(
    Companies=[
        {"name": "Name", "type": "singleLineText"},
        {"name": "Channel", "type": "singleLineText"},
    ],
    Applications=[
        {"name": "Tracker ID", "type": "singleLineText"},
        {"name": "Role", "type": "singleLineText"},
        {"name": "Sent", "type": "date", "options": {"dateFormat": {"name": "iso"}}},
        {"name": "Status", "type": "singleSelect", "options": choices(STATUSES)},
        {"name": "Pay PHP per month", "type": "number", "options": {"precision": 0}},
        {"name": "Pay as listed", "type": "singleLineText"},
        {"name": "Setup", "type": "singleLineText"},
        {"name": "Work mode", "type": "singleSelect", "options": choices(MODES)},
        {"name": "Channel", "type": "singleLineText"},
        {"name": "Latest", "type": "multilineText"},
        {"name": "Your move", "type": "multilineText"},
    ],
    Events=[
        {"name": "What", "type": "singleLineText"},
        {"name": "Date", "type": "date", "options": {"dateFormat": {"name": "iso"}}},
    ],
)
LINKS = [  # (table, field name, linked table)
    ("Applications", "Company", "Companies"),
    ("Events", "Application", "Applications"),
]


class Airtable:
    def __init__(self, token: str, base: str, dry_run: bool = False):
        self.base = base
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._last = 0.0

    def call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        if self.dry_run and method != "GET":
            print(f"  [dry run] {method} {path} ({len((body or {}).get('records', [])) or 1} item(s))")
            return {"records": [], "id": "dry-run"}
        for attempt in range(6):
            wait = 0.22 - (time.monotonic() - self._last)  # stay under 5 req/s
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = self.s.request(method, f"{API}{path}", json=body, params=params, timeout=30)
            if r.status_code == 429:
                print("  rate limited, waiting 30 s")
                time.sleep(30)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise SystemExit(f"{method} {path} failed: {r.status_code} {r.text[:300]}")
            return r.json()
        raise SystemExit(f"{method} {path} kept failing after retries")

    # --- schema ---
    def tables(self) -> dict[str, dict]:
        data = self.call("GET", f"/meta/bases/{self.base}/tables")
        return {t["name"]: t for t in data["tables"]}

    def ensure_schema(self) -> dict[str, dict]:
        existing = self.tables()
        for name, fields in SCHEMA.items():
            if name not in existing:
                print(f"Creating table {name}")
                self.call("POST", f"/meta/bases/{self.base}/tables", {"name": name, "fields": fields})
            else:
                have = {f["name"] for f in existing[name]["fields"]}
                for f in fields:
                    if f["name"] not in have:
                        print(f"Adding field {name}.{f['name']}")
                        self.call("POST", f"/meta/bases/{self.base}/tables/{existing[name]['id']}/fields", f)
        existing = self.tables() if not self.dry_run else existing
        for table, field, target in LINKS:
            if table in existing and target in existing:
                if field not in {f["name"] for f in existing[table]["fields"]}:
                    print(f"Adding link {table}.{field} -> {target}")
                    self.call("POST", f"/meta/bases/{self.base}/tables/{existing[table]['id']}/fields",
                              {"name": field, "type": "multipleRecordLinks",
                               "options": {"linkedTableId": existing[target]["id"]}})
        return self.tables() if not self.dry_run else existing

    # --- records ---
    def upsert(self, table: str, records: list[dict], merge_on: list[str]) -> dict[str, str]:
        """Upsert records; return {merge value: record id}."""
        ids: dict[str, str] = {}
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            data = self.call("PATCH", f"/{self.base}/{requests.utils.quote(table)}", {
                "performUpsert": {"fieldsToMergeOn": merge_on},
                "records": [{"fields": f} for f in chunk],
                "typecast": True,
            })
            for rec in data.get("records", []):
                ids[str(rec["fields"].get(merge_on[0]))] = rec["id"]
        return ids


def clean(fields: dict) -> dict:
    """Drop empty values so an upsert never blanks a field someone filled in Airtable."""
    return {k: v for k, v in fields.items() if v not in ("", None, [])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("json_path", help="tracker export: a JSON array of applications")
    ap.add_argument("--base", required=True, help="Airtable base id (starts with app)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()

    token = os.environ.get("AIRTABLE_TOKEN")
    if not token:
        sys.exit("Set AIRTABLE_TOKEN to an Airtable personal access token first.")
    if not args.base.startswith("app"):
        sys.exit("--base should be the base id from the URL, starting with 'app'.")

    with open(args.json_path, encoding="utf-8") as fh:
        apps = json.load(fh)
    print(f"{len(apps)} applications in {args.json_path}")

    at = Airtable(token, args.base, dry_run=args.dry_run)
    at.ensure_schema()

    # Companies: one per company name, channel from its first application.
    companies: dict[str, dict] = {}
    for a in apps:
        companies.setdefault(a["company"], clean({"Name": a["company"], "Channel": a["channel"]}))
    company_ids = at.upsert("Companies", list(companies.values()), ["Name"])
    print(f"Companies upserted: {len(companies)}")

    app_records = []
    for a in apps:
        f = clean({
            "Tracker ID": a["id"], "Role": a["role"], "Sent": a["sent"], "Status": a["status"],
            "Pay PHP per month": a.get("pay_php"), "Pay as listed": a.get("pay"), "Setup": a.get("setup"),
            "Work mode": a.get("work_mode"), "Channel": a["channel"], "Latest": a.get("next_step"),
            "Your move": a.get("your_move"),
        })
        if a["company"] in company_ids:
            f["Company"] = [company_ids[a["company"]]]
        if not a.get("your_move"):
            f["Your move"] = ""  # a finished to-do should clear in Airtable too
        app_records.append(f)
    app_ids = at.upsert("Applications", app_records, ["Tracker ID"])
    print(f"Applications upserted: {len(app_records)}")

    events = []
    for a in apps:
        if a.get("due"):
            f = {"What": a["due_what"], "Date": a["due"]}
            if a["id"] in app_ids:
                f["Application"] = [app_ids[a["id"]]]
            events.append(f)
    if events:
        at.upsert("Events", events, ["What"])
    print(f"Events upserted: {len(events)}")
    print("Done." if not args.dry_run else "Dry run finished; nothing was written.")


if __name__ == "__main__":
    main()
