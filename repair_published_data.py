#!/usr/bin/env python3
"""
repair_published_data.py  —  apply data_checks to an already-built dataset
=========================================================================
The proper fix for bad data is a fresh live run with the new rules:

    python sponsor_pipeline.py --real --api --llm
    python export_sponsor_site.py

That can find the RIGHT Companies House company where the old run linked the
wrong one. This script is the stopgap for when you can't re-run the APIs: it
reads the data embedded in a published lab.html, applies the same rules
(data_checks.py), re-scores with the same engine, and re-exports with the same
exporter. It only ever REMOVES or RE-LABELS what it can't trust; it never adds
facts. A rejected Companies House match is unlinked (age/size become unknown,
scored neutral), not replaced with a guess.

    python repair_published_data.py --src lab.html --out site

Outreach status/notes in the source page are carried over untouched.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import yaml

import data_checks as dc
import sponsor_db as db
import sponsor_scoring as ss
from sponsor_pipeline import dedupe_candidates


def load_published(path):
    html = open(path, encoding="utf-8").read()
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.S)
    if not m:
        raise SystemExit(f"no embedded DATA found in {path}")
    return json.loads(m.group(1).replace("<\\/", "</"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="index.html",
                    help="a published page that carries the website readings (index.html since Phase 2, or an old lab.html)")
    ap.add_argument("--out", default="site")
    ap.add_argument("--db", default="sponsor_repaired.db")
    a = ap.parse_args()

    cfg = ss.load_config()
    data = load_published(a.src)
    if not data.get("lab") and not any(b.get("soft") for b in data["businesses"]):
        raise SystemExit("this page has no website readings: use a Phase 2 index.html or an old lab.html")
    rows = data["businesses"]

    stats = dict(total=len(rows), outside=0, unlinked=0, chains=0, resectored=0)
    recs = []
    for b in rows:
        rec = dict(
            name=b["name"], sector=b["sector"], distance_km=b["distance_km"],
            company_age=b["company_age"] or 0, size_band=b["size_band"] or "unknown",
            accounts_dormant=bool(b["dormant"]), website=b["website"], address=b["address"],
            company_number=b["company_number"], postcode=b["postcode"], ch_flag=b["ch_flag"],
            warm_intro=b["warm_intro"], source="places+companies_house",
            _soft=b.get("soft") or {}, _status=(b["status"], b["owner"], b["notes"]),
        )
        if not dc.within_radius(rec, cfg):
            stats["outside"] += 1
            continue
        before_cn, before_sector = rec["company_number"], rec["sector"]
        dc.reconcile(rec, cfg)
        if before_cn and not rec["company_number"] and rec["sector"] != "national_chain":
            stats["unlinked"] += 1
        if rec["sector"] == "national_chain":
            stats["chains"] += 1
        elif rec["sector"] != before_sector:
            stats["resectored"] += 1
        recs.append(rec)

    recs, dropped = dedupe_candidates(recs)
    table = dc.load_connections(cfg)
    for rec in recs:
        dc.apply_connections(rec, cfg, table)
    stats["connected"] = sum(1 for r in recs if r.get("connection_kind"))

    if os.path.exists(a.db):
        os.remove(a.db)
    conn = db.connect(a.db)
    db.init_db(conn)
    score_rows = []
    for rec in recs:
        soft = rec["_soft"]
        flat = dict(rec)
        for f, (vk, ck) in {"community_affinity": ("community_affinity", "community_conf"),
                            "brand_fit": ("brand_fit", "brand_conf"),
                            "csr_signal": ("csr_signal", "csr_conf")}.items():
            s = soft.get(f) or {"value": 0.5, "confidence": 0.0}
            flat[vk], flat[ck] = s["value"], s["confidence"]
        r = ss.score_business(flat, cfg)
        flags = ss.flags_for(flat, cfg)
        if rec.get("ch_flag"):
            flags = (flags + "; " if flags != "-" else "") + rec["ch_flag"]
        score_rows.append(dict(
            business_id=rec["business_id"], fit_score=r["fit_score"],
            top_driver=max(r["contrib"], key=r["contrib"].get).replace("_", " "),
            contrib=r["contrib"], tier=ss.recommend_tier(rec["sector"], cfg, rec.get("connection_kind"), rec.get("size_band")),
            flags=flags))
        if soft:
            model = next(iter(soft.values())).get("model", "unknown")
            db.upsert_soft_features(conn, rec["business_id"],
                                    {f: dict(value=v["value"], confidence=v["confidence"],
                                             justification=v["justification"]) for f, v in soft.items()},
                                    model)
    db.upsert_businesses(conn, recs)
    db.save_scores(conn, score_rows, config_version="v2-datachecks")
    # keep the page's "Data to" date = when the data was gathered, not today
    if data.get("updated"):
        conn.execute("UPDATE business_scores SET computed_at=?", (data["updated"] + "T00:00:00",))
    for rec in recs:
        status, owner, notes = rec["_status"]
        if status and status != "new":
            db.set_status(conn, rec["business_id"], status, owner or "", notes or "")
    conn.commit()
    conn.close()

    subprocess.run([sys.executable, "export_sponsor_site.py", "--db", a.db, "--out", a.out], check=True)

    kept = len(recs)
    print(f"\nrepair summary (source: {a.src}, {stats['total']} businesses)")
    print(f"  dropped, outside {cfg['ingest']['places']['max_distance_km']} km : {stats['outside']}")
    print(f"  Companies House match unlinked (wrong place): {stats['unlinked']}")
    print(f"  re-classed as national chain / franchise : {stats['chains']}")
    print(f"  sector corrected from business name      : {stats['resectored']}")
    print(f"  duplicate branches merged                : {dropped}")
    print(f"  with a recorded local connection         : {stats['connected']}")
    print(f"  kept: {stats['total']} - {stats['outside']} - {dropped} = {kept}")


if __name__ == "__main__":
    main()
