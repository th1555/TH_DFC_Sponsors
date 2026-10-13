#!/usr/bin/env python3
"""
sponsor_pipeline.py  —  build the sponsor store end to end (steps 1+2+3)
=======================================================================
The single orchestrator, analogous to store_pipeline.py on the recruitment side.
For every candidate business it runs the whole chain and persists the result:

    website text + facts
        -> soft_features.enrich()      (step 2: the 3 LLM soft judgments)
        -> sponsor_scoring.score()     (step 1: deterministic 0-100 fit score)
        -> sponsor_db                  (step 3: businesses + soft_features + scores)

Re-running is safe: businesses/soft_features upsert, business_scores is rebuilt,
and outreach_status (the human's notes) is never touched.

Two candidate sources:
  synthetic (default) — generated businesses with realistic website copy; offline.
  --real              — discover + enrich REAL businesses near the ground via
                        ingest_places + ingest_companies_house + fetch_website.
                        Offline by default (fixtures); add --api for the live APIs.
And two soft-feature modes: stub (default, offline) or --llm (real model).

Run:  python sponsor_pipeline.py                  # synthetic, offline
      python sponsor_pipeline.py --real           # real businesses, fixtures
      python sponsor_pipeline.py --real --api --llm   # fully live (needs 3 keys)

Live keys: GOOGLE_PLACES_API_KEY, COMPANIES_HOUSE_API_KEY, ANTHROPIC_API_KEY.
"""
from __future__ import annotations
import sys
import numpy as np
import sponsor_scoring as ss
import soft_features as sf
import sponsor_db as db
import ingest_places as places
import ingest_companies_house as ch
import fetch_website as web

RNG = np.random.default_rng(7)

# templated website copy by sector, so the stub/LLM has real text to judge.
# local sectors get community-flavoured copy; national/online get the opposite.
_COPY = {
    "transport_coaches_haulage": "A family-run local coach and haulage firm serving the town for years, proud to support community groups.",
    "building_construction_trades": "Local independent builders and joiners, established in the area, trusted by families across the district.",
    "pubs_bars_hospitality": "A community local at the heart of the town — matchdays, quiz nights and a proud part of local life.",
    "motor_dealers_garages": "Independent local garage and MOT centre, owner-run, serving drivers in the town for two decades.",
    "local_professional_services": "A local accountancy practice supporting small businesses across the town and surrounding area.",
    "funeral_directors": "An independent, family-owned funeral director rooted in the local community for generations.",
    "food_takeaway_restaurants": "A local independent takeaway, a familiar name on the high street, popular with matchday crowds.",
    "utilities_renewables_heating": "Local heating and renewables installers, family firm, trusted trades serving homes across the area.",
    "health_fitness_physio_dental": "A community physio and fitness clinic supporting local sports teams and residents.",
    "independent_retail": "An independent local shop on the high street, a family business proud of its roots in the town.",
    "national_chain": "Part of a nationwide retail group. UK-wide stores, national brand, standard range and pricing across all locations.",
    "online_ecommerce": "We ship nationwide, delivered to your door. UK-wide next-day delivery on thousands of online-only deals.",
    "public_sector_charity": "A registered charity operating across the region, funded by grants and public donations.",
}


# Invented names for the demo pool. Fictional on purpose: the site labels demo
# data as such, and nothing here should read as a claim about a real firm.
_DEMO_NAMES = {
    "transport_coaches_haulage": ["Levenside Haulage", "Clyde Valley Coaches", "Rockview Transport"],
    "building_construction_trades": ["Castle Rock Builders", "Kilpatrick Joinery", "Westbank Roofing"],
    "pubs_bars_hospitality": ["The Boghead Bar", "Levengrove Arms", "The Quayside Tavern"],
    "motor_dealers_garages": ["Overtoun Motors", "Brucehill Garage & MOT", "Riverside Car Sales"],
    "local_professional_services": ["Glencairn & Co Accountants", "Church Street Solicitors", "Rock Estate Agents"],
    "funeral_directors": ["Vale Funeral Services", "Kirkland Funeral Directors"],
    "food_takeaway_restaurants": ["High Street Fish Bar", "Castle Spice Takeaway", "The Lennox Cafe"],
    "utilities_renewables_heating": ["Leven Heating & Plumbing", "Clyde Solar Installs", "Westshore Electrical"],
    "health_fitness_physio_dental": ["Rock Physio & Sports Injury", "Dalreoch Dental", "Levenford Fitness"],
    "independent_retail": ["Kirk Hardware", "Lennox Flowers", "Bridge Street Butchers"],
    "national_chain": ["MegaMart Superstore", "QuickFit Tyres (national)"],
    "online_ecommerce": ["ByteDeals Online", "ParcelPrime Direct"],
    "public_sector_charity": ["West Dunbartonshire Leisure Trust", "Vale Community Foundation"],
}
_DEMO_STREETS = ["High Street", "Glasgow Road", "Church Street", "College Street",
                 "Castle Street", "Main Street", "Bonhill Road", "Cardross Road"]
_DEMO_TOWNS = [("Dumbarton", 0, 3), ("Alexandria", 3, 6), ("Bonhill", 3, 6),
               ("Renton", 2, 4), ("Clydebank", 6, 12), ("Helensburgh", 9, 14)]


def _demo_address(dist):
    towns = [t for t, lo, hi in _DEMO_TOWNS if lo <= dist < hi] or ["Dumbarton"]
    return f"{int(RNG.integers(2, 140))} {RNG.choice(_DEMO_STREETS)}, {RNG.choice(towns)}"


def make_candidates(n_random=26):
    """Demo businesses WITH website text (the real enrichment input). Starts with
    the 4 hand-written demo firms, then fills out a realistic candidate pool."""
    cands = [dict(r) for r in sf.DEMO]      # 4 legible, hand-written firms
    # the hand-written fixtures borrow some real local names; on the demo site
    # every name is fictional, so invented copy is never pinned on a real firm
    rename = {"Marbill Coaches": "Rockside Coaches", "Bonhill Funeralcare": "Bonhill Family Funerals",
              "ByteBargains Online": "ByteBargains Direct", "Riverside Physio": "Riverside Physio Clinic"}
    for r in cands:
        old = r["name"]
        r["name"] = rename.get(old, old)
        for k in ("website_text",):
            if r.get(k):
                r[k] = r[k].replace(old.split()[0], r["name"].split()[0])
        r.setdefault("size_band", "small")
        r["address"] = _demo_address(r["distance_km"])
        r["website"] = "https://example.com" if r.get("website_text") else None
        r["source"] = "demo"

    used = {r["name"] for r in cands}
    sectors = list(_COPY.keys())
    sizes = ["micro", "small", "medium", "large"]
    for i in range(n_random):
        sector = str(RNG.choice(sectors))
        free = [n for n in _DEMO_NAMES[sector] if n not in used]
        if not free:
            continue
        name = free[0]; used.add(name)
        # ~10% of firms get a thin/empty site -> low confidence downstream
        text = "" if RNG.random() < 0.10 else _COPY[sector]
        dist = float(np.clip(RNG.exponential(4.0), 0.3, 18))
        cands.append(dict(
            name=name, sector=sector, distance_km=dist,
            company_age=int(np.clip(RNG.normal(14, 9), 1, 60)),
            size_band=str(RNG.choice(sizes, p=[0.45, 0.35, 0.15, 0.05])),
            accounts_dormant=bool(RNG.random() < 0.05),
            website_text=text, warm_intro=None, source="demo",
            address=_demo_address(dist),
            website=None if not text else "https://example.com",
        ))
    return cands


def ingest_real(cfg, ingest_mode="fixture"):
    """The real-data path: discover businesses near the ground (Places), enrich
    each via Companies House, fetch its homepage text. Returns records in the
    SAME shape make_candidates() produces, so the rest of the pipeline is
    identical. ingest_mode='api' uses the live APIs (needs the three keys)."""
    web_mode = "http" if ingest_mode == "api" else "fixture"
    cands = []
    for p in places.discover(cfg, mode=ingest_mode):
        facts = ch.enrich(p["name"], cfg, mode=ingest_mode)
        cands.append(dict(
            name=p["name"], sector=facts["sector"],
            distance_km=p.get("distance_km", 0.0),
            company_age=facts["company_age"], size_band=facts["size_band"],
            accounts_dormant=facts["accounts_dormant"],
            website_text=web.fetch(p.get("website"), mode=web_mode),
            warm_intro=None,
            source="places+companies_house" if ingest_mode == "api" else "fixture",
            company_number=facts.get("company_number"), ch_flag=facts.get("ch_flag"),
            website=p.get("website"), address=p.get("address"),
            postcode=facts.get("postcode")))
    return cands


def run(mode="stub", source="synthetic", ingest_mode="fixture"):
    cfg = ss.load_config()
    conn = db.connect(); db.init_db(conn)
    model = sf.DEFAULT_MODEL if mode == "llm" else "stub-keyword-v1"

    cands = ingest_real(cfg, ingest_mode) if source == "real" else make_candidates()
    biz_records, score_rows = [], []
    for rec in cands:
        rec["business_id"] = db.business_key(rec)
        flat = sf.enrich(rec, mode=mode)                     # step 2
        r = ss.score_business(flat, cfg)                     # step 1
        top = max(r["contrib"], key=r["contrib"].get)

        flags = ss.flags_for(flat, cfg)
        if rec.get("ch_flag"):                               # carry CH match caveats through
            flags = (flags + "; " if flags != "-" else "") + rec["ch_flag"]
        biz_records.append(rec)
        db.upsert_soft_features(conn, rec["business_id"], flat["_soft_derived"], model)
        score_rows.append(dict(
            business_id=rec["business_id"], fit_score=r["fit_score"],
            top_driver=top.replace("_", " "),
            contrib=r["contrib"], tier=ss.recommend_tier(rec["sector"], cfg),
            flags=flags))

    db.upsert_businesses(conn, biz_records)                  # step 3
    db.save_scores(conn, score_rows, config_version="v1")

    # Demo only: seed two statuses so the outreach column isn't empty. Never on
    # real data, and never once anyone has recorded a status: on a live run this
    # would overwrite real notes, which is exactly what the wall exists to stop.
    has_notes = conn.execute("SELECT COUNT(*) FROM outreach_status").fetchone()[0]
    if source == "synthetic" and not has_notes:
        q = db.get_queue(conn)
        if len(q) >= 4:
            db.set_status(conn, q.iloc[0].business_id, "to-approach",
                          owner="Club board", notes="Warm path via board — call first.")
            db.set_status(conn, q.iloc[3].business_id, "contacted",
                          owner="Tom", notes="Emailed 12 Sep, awaiting reply.")
    return conn


if __name__ == "__main__":
    mode = "llm" if "--llm" in sys.argv else "stub"
    source = "real" if "--real" in sys.argv else "synthetic"
    # --api uses the LIVE Places/Companies House APIs; default is offline fixtures
    ingest_mode = "api" if "--api" in sys.argv else "fixture"
    print("=" * 74)
    print(f"SPONSOR PIPELINE  —  build the store end to end")
    print(f"  source: {source} ({ingest_mode})   |   soft features: {mode}")
    print("=" * 74)
    if mode == "llm" and not __import__("os").environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY — using offline stub for soft features.\n")
        mode = "stub"

    conn = run(mode, source=source, ingest_mode=ingest_mode)
    q = db.get_queue(conn)
    print(f"\nbuilt sponsor.db  —  {len(q)} businesses scored & stored\n")
    print("TOP OF THE STORED QUEUE (what the app opens on):\n")
    print(q[["name", "sector", "fit_score", "tier", "status", "flags"]]
          .head(10).to_string(index=False))

    print("\n--- provenance check: soft features carry model + timestamp ---")
    bid = q.iloc[0].business_id
    _, sfeat, contrib = db.get_detail(conn, bid)
    print(f"{q.iloc[0]['name']}  (fit {q.iloc[0].fit_score}):")
    print(sfeat[["feature", "value", "confidence", "model", "justification"]]
          .to_string(index=False))

    print("\n--- the WALL: re-scoring must not touch human notes ---")
    before = db.get_queue(conn)[["business_id", "status", "notes"]]
    before = before[before.status != "new"]
    # re-run the score writer only (simulating a data refresh), carrying the real
    # per-feature points across so the check itself doesn't damage the scores
    import json as _json
    contrib = dict(conn.execute("SELECT business_id, contrib_json FROM business_scores"))
    rows = [dict(business_id=r["business_id"], fit_score=r["fit_score"],
                 top_driver=r["top_driver"], contrib=_json.loads(contrib[r["business_id"]]),
                 tier=r["tier"], flags=r["flags"])
            for _, r in db.get_queue(conn).iterrows()]
    db.save_scores(conn, rows)
    after = db.get_queue(conn)[["business_id", "status", "notes"]]
    after = after[after.status != "new"]
    kept = before.merge(after, on=["business_id", "status", "notes"])
    print(f"human rows before refresh: {len(before)}  |  survived refresh: {len(kept)}")
    print("outreach_status is untouched by re-scoring — exactly the point.")
