#!/usr/bin/env python3
"""
export_sponsor_site.py  —  build the shareable sponsor website from sponsor.db
=============================================================================
The sponsor-side twin of export_site.py. Reads the finished store (businesses,
scores, website readings, employees and revenue, outreach status) and writes
one self-contained hub page from the design in sponsor_site.html:

    site/index.html   <- everything, every value tagged by where it came from:
                         Reported / Estimate / AI reading / Possible lead /
                         Not available
    site/lab.html     <- a redirect to index.html (the lab page was merged into
                         the hub in Phase 2; old bookmarks still land)

    python sponsor_pipeline.py --real --api      # build the store
    python export_sponsor_site.py                 # write site/
    python export_sponsor_site.py --out ../TH_DFC/site/sponsors --scouting-link ../

The second form drops the pages into the Sons Scouting site, so one Cloudflare
Access login covers both and the header links back to Forwards.

Deps: pandas, pyyaml (+ sponsor_db.py, sponsor_config.yaml, a built sponsor.db)
"""
from __future__ import annotations
import argparse
import json
import math
import os
import shutil
import sqlite3
import pandas as pd
import yaml
import sponsor_db as db
import data_checks as dc

TEMPLATE = "sponsor_site.html"
REDIRECT = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="robots" content="noindex, nofollow"><meta http-equiv="refresh" content="0; url=index.html">
<title>Sons Sponsors</title></head><body style="font-family:sans-serif;padding:24px">
The lab page is now part of the main sponsor page. <a href="index.html">Go to Sons Sponsors</a>.
</body></html>
"""
MARK = "/*__DATA__*/null"
HARD = ["proximity", "sector_propensity", "company_stability", "ability_to_pay"]
FACT = ["proximity", "company_stability", "ability_to_pay"]   # records only: no hand-set sector rating
SOFT = ["community_affinity", "brand_fit", "csr_signal"]

SECTOR_LABELS = {
    "transport_coaches_haulage": "Coaches & haulage",
    "industrial_b2b_services": "Industry & business services",
    "building_construction_trades": "Building & trades",
    "pubs_bars_hospitality": "Pubs & hospitality",
    "motor_dealers_garages": "Motor dealers & garages",
    "local_professional_services": "Professional services",
    "funeral_directors": "Funeral directors",
    "food_takeaway_restaurants": "Food & takeaways",
    "utilities_renewables_heating": "Heating, power & renewables",
    "health_fitness_physio_dental": "Health & fitness",
    "independent_retail": "Independent retail",
    "national_chain": "National chains",
    "online_ecommerce": "Online retailers",
    "public_sector_charity": "Public sector & charity",
    "unknown": "Unclassified",
}


def clean(v):
    """JSON-safe scalar (NaN/None -> None, numpy -> python)."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v


def as_int(v):
    v = clean(v)
    return int(v) if v is not None else None


def approach(tier: str) -> dict:
    """'B2B — programme, website, training-wear' -> {kind, detail}."""
    head, _, tail = str(tier or "").partition(" — ")
    return {"kind": head.strip() or "Review", "detail": tail.strip()}


def ons_rows_used(est: pd.DataFrame) -> list[dict]:
    """The ONS lines behind the estimates on this page, for the methods card."""
    import revenue_estimate as rev
    used = est[est.revenue_basis == "estimate"]
    if used.empty:
        return []
    ratios = rev.load_ratios()
    pairs = {(sec, rev.find_ratio(ratios, sec, int(n))["size_band"])
             for sec, n in zip(used.est_section, used.employees)
             if rev.find_ratio(ratios, sec, int(n))}
    keep = []
    for r in ratios:
        if (r["section"], r["size_band"]) in pairs:
            keep.append(dict(section=r["section"], name=rev.SECTION_NAMES.get(r["section"], r["section_name"]),
                             band=r["size_band"].replace(" to ", "–"),
                             avg=round(r["avg_turnover_per_business_gbp"]),
                             businesses=int(r["enterprises"]), year=r["year"],
                             coverage=r.get("coverage_note") or ""))
    return keep


def build_payload(conn, cfg, scouting_link: str | None) -> dict:
    w = cfg["weights"]
    fact_total = sum(w[k] for k in FACT)

    biz = pd.read_sql("SELECT * FROM businesses", conn)
    sc = pd.read_sql("SELECT * FROM business_scores", conn)
    os_ = pd.read_sql("SELECT * FROM outreach_status", conn)
    sf = pd.read_sql("SELECT * FROM soft_features", conn)
    est = pd.read_sql("SELECT * FROM revenue_estimates", conn).drop(columns=["computed_at"])
    df = (sc.merge(biz, on="business_id", how="left")
            .merge(os_, on="business_id", how="left")
            .merge(est.rename(columns={"employees": "est_employees"}), on="business_id", how="left"))

    soft_by = {}
    for r in sf.itertuples():
        soft_by.setdefault(r.business_id, {})[r.feature] = dict(
            value=round(float(r.value), 3), confidence=round(float(r.confidence), 3),
            justification=r.justification, model=r.model)

    rows = []
    for i, r in enumerate(df.sort_values("fit_score", ascending=False).itertuples()):
        contrib = json.loads(r.contrib_json) if r.contrib_json else {}
        local_fit = 100 * sum(contrib.get(k, 0) for k in FACT) / fact_total
        # things to check before calling; the warm path and "micro business" are
        # shown elsewhere (tag, size column) and aren't warnings. AI leads are
        # split out: they're shown as "Possible lead", not as a check.
        raw = [f.strip() for f in str(r.flags or "").split(";")
               if f.strip() and f.strip() != "-"
               and not f.strip().startswith(("WARM PATH", "micro business"))]
        leads = [f for f in raw if f.startswith("possible connection")]
        flags = [f for f in raw if f not in leads]
        # did a recorded connection, rather than distance, set the proximity part?
        dist_pts = (math.exp(-float(r.distance_km) / cfg["proximity"]["decay_km"]) * w["proximity"]
                    if pd.notna(r.distance_km) else 0.0)
        via = (clean(r.connection_note)
               if contrib.get("proximity", 0) > dist_pts + 0.01 and clean(r.connection_note) else None)
        rec = dict(
            n=i, id=r.business_id, name=clean(r.name), sector=clean(r.sector),
            sector_label=SECTOR_LABELS.get(r.sector, r.sector),
            distance_km=round(float(r.distance_km), 2) if pd.notna(r.distance_km) else None,
            company_age=clean(r.company_age), size_band=clean(r.size_band),
            dormant=bool(r.accounts_dormant), address=clean(r.address),
            website=clean(r.website), company_number=clean(r.company_number),
            postcode=clean(r.postcode), ch_flag=clean(r.ch_flag),
            warm_intro=clean(r.warm_intro), approach=approach(r.tier), flags=flags, leads=leads,
            local_fit=round(local_fit, 1),
            parts={k: dict(points=round(contrib.get(k, 0), 2), weight=w[k],
                           **({"via": via} if k == "proximity" and via else {}))
                   for k in HARD},
            connection=(dict(kind=clean(r.connection_kind), note=clean(r.connection_note))
                        if clean(r.connection_kind) else None),
            google_rating=clean(getattr(r, "google_rating", None)),
            google_reviews=as_int(getattr(r, "google_reviews", None)),
            directors_local=as_int(getattr(r, "directors_local", None)),
            directors_total=as_int(getattr(r, "directors_total", None)),
            status=clean(r.status) or "new", owner=clean(r.owner), notes=clean(r.notes),
            status_at=clean(r.updated_at),
            # employees + revenue: recorded where filed, else a labelled estimate
            # with its working, else not available (never a guess)
            size=dict(
                employees=as_int(r.est_employees), employees_band=clean(r.employees_band),
                employees_basis=clean(r.employees_basis) or "not_available",
                employees_note=clean(r.employees_note) or "Not read yet",
                revenue_basis=clean(r.revenue_basis) or "not_available",
                revenue_band=clean(r.revenue_band), revenue_value=as_int(r.revenue_value),
                revenue_working=clean(r.revenue_working),
                est_avg_turnover=as_int(r.est_avg_turnover), est_source=clean(r.est_source),
                accounts_period_end=clean(r.accounts_period_end),
                accounts_source=clean(r.accounts_source)),
        )
        rec["fit"] = round(float(r.fit_score), 1)
        rec["soft_parts"] = {k: dict(points=round(contrib.get(k, 0), 2), weight=w[k]) for k in SOFT}
        rec["soft"] = soft_by.get(r.business_id, {})
        rows.append(rec)

    # rows of connections.csv that matched no business on the page (most real
    # sponsors aren't in the Places search): carried so the in-page download
    # of the sponsor list never loses them
    table = dc.load_connections(cfg)
    unmatched = []
    for row in table:
        key = row["match"].strip()
        hit = any((str(b["company_number"] or "").upper() == key.upper())
                  or dc._same_business(key, b["name"]) for b in rows)
        if not hit:
            unmatched.append(dict(match=key, kind=row.get("kind", ""), note=row.get("note", "")))

    sources = set(biz.source.dropna())
    models = sorted(set(sf.model.dropna()))
    updated = str(sc.computed_at.max())[:10] if len(sc) else None
    nav = [{"label": "Sponsors", "href": "#", "current": True}]
    if scouting_link:
        nav.insert(0, {"label": "Forwards", "href": scouting_link, "current": False})
    return dict(
        updated=updated,
        demo=not sources or sources <= {"demo", "synthetic", "fixture"},
        soft_models=models,
        stadium=cfg.get("ingest", {}).get("stadium", {}).get("name", "the ground"),
        weights={k: w[k] for k in HARD + SOFT},
        sectors=[dict(key=k, label=SECTOR_LABELS.get(k, k), propensity=v)
                 for k, v in cfg["sector_propensity"].items() if k != "unknown"],
        nav=nav, businesses=rows,
        connection_strength=(cfg.get("connections") or {}).get("strength", {}),
        decay_km=cfg["proximity"]["decay_km"],
        connections_unmatched=unmatched,
        offers_source=(cfg.get("offers") or {}).get("source"),
        ons=ons_rows_used(est),
        accounts_read=bool(len(est)),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", default=db.DB_PATH)
    ap.add_argument("--config", default="sponsor_config.yaml")
    ap.add_argument("--out", default="site")
    ap.add_argument("--scouting-link", default=None,
                    help="href back to the Sons Scouting forwards page, e.g. ../")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        raise SystemExit(f"No {a.db} here. Run sponsor_pipeline.py first.")
    cfg = yaml.safe_load(open(a.config))
    conn = sqlite3.connect(a.db)
    html = open(TEMPLATE, encoding="utf-8").read()
    if MARK not in html:
        raise SystemExit(f"{TEMPLATE} is missing the data marker {MARK}")

    os.makedirs(a.out, exist_ok=True)
    data = build_payload(conn, cfg, a.scouting_link)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")              # safe inside <script>
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html.replace(MARK, payload, 1))
    with open(os.path.join(a.out, "lab.html"), "w", encoding="utf-8") as f:
        f.write(REDIRECT)
    conn.close()
    if os.path.exists("crest.png"):
        shutil.copy("crest.png", os.path.join(a.out, "crest.png"))

    kb = os.path.getsize(os.path.join(a.out, "index.html")) // 1024
    print(f"wrote {a.out}/index.html ({kb} KB) and a lab.html redirect: "
          f"{len(data['businesses'])} businesses, data as of {data['updated']}"
          f"{'  [DEMO DATA]' if data['demo'] else ''}")


if __name__ == "__main__":
    main()
