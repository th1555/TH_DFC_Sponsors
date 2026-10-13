#!/usr/bin/env python3
"""
outreach.py  —  record who has been approached, for the sponsor site
====================================================================
The shareable site is a static page (like Sons Scouting), so nobody edits it in
the browser. Outreach status lives in sponsor.db's walled human table instead,
and this is the way to change it. It never touches scores or business data,
and a pipeline refresh never touches what it writes.

    python outreach.py list                       # everything with a status
    python outreach.py find coach                 # look up a business by name
    python outreach.py set "Rockside Coaches" contacted --owner Tom --note "Emailed 14 Oct"
    python outreach.py set "Rockside Coaches" won --note "Programme back page, 2026/27"

Statuses: new, to-approach, contacted, in-talks, won, dead (shown as "Not now").
Then rebuild the site:  python export_sponsor_site.py
"""
from __future__ import annotations
import argparse
import sys
import sponsor_db as db


def find(conn, text):
    q = db.get_queue(conn)
    t = text.lower().strip()
    exact = q[q.name.str.lower() == t]
    return exact if len(exact) else q[q.name.str.lower().str.contains(t, regex=False)]


def main():
    ap = argparse.ArgumentParser(description="Record sponsor outreach status.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show every business with a status")
    f = sub.add_parser("find", help="look up businesses by name")
    f.add_argument("name")
    s = sub.add_parser("set", help="set a business's status")
    s.add_argument("name", help="business name, or enough of it to be unique")
    s.add_argument("status", choices=db.STATUSES)
    s.add_argument("--owner", default=None, help="who is handling it")
    s.add_argument("--note", default=None, help="replaces the current note")
    a = ap.parse_args()

    conn = db.connect(); db.init_db(conn)
    if a.cmd == "list":
        q = db.get_queue(conn)
        q = q[q.status != "new"]
        print(q[["name", "status", "owner", "notes", "updated_at"]].to_string(index=False)
              if len(q) else "No outreach recorded yet.")
        return
    hits = find(conn, a.name)
    if a.cmd == "find":
        print(hits[["name", "sector", "fit_score", "status"]].to_string(index=False)
              if len(hits) else "No match.")
        return
    if len(hits) != 1:
        print(f"'{a.name}' matches {len(hits)} businesses. Be more specific:" if len(hits)
              else f"No business matches '{a.name}'.")
        if len(hits):
            print(hits[["name", "sector"]].to_string(index=False))
        sys.exit(1)
    row = hits.iloc[0]
    owner = a.owner if a.owner is not None else (row.owner or "")
    note = a.note if a.note is not None else (row.notes or "")
    db.set_status(conn, row.business_id, a.status, owner=owner, notes=note)
    print(f"{row['name']}: {row.status} -> {a.status}"
          f"{'  (owner ' + owner + ')' if owner else ''}. Run export_sponsor_site.py to publish.")


if __name__ == "__main__":
    main()
