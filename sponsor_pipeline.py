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

Runs offline on synthetic businesses that carry realistic website copy so the
soft-feature layer has something to read. `--llm` derives soft features with the
real model instead (needs ANTHROPIC_API_KEY).

Run:  python sponsor_pipeline.py            # offline, builds sponsor.db
      python sponsor_pipeline.py --llm      # real soft-feature derivation
"""
from __future__ import annotations
import sys
import numpy as np
import sponsor_scoring as ss
import soft_features as sf
import sponsor_db as db

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


def make_candidates(n_random=22):
    """Businesses WITH website text (the real enrichment input). Starts with the
    4 hand-written demo firms, then fills out a realistic candidate pool."""
    cands = [dict(r) for r in sf.DEMO]      # 4 legible, hand-written firms
    for r in cands:
        r.setdefault("size_band", "small")

    sectors = list(_COPY.keys())
    sizes = ["micro", "small", "medium", "large"]
    for i in range(n_random):
        sector = str(RNG.choice(sectors))
        # ~10% of firms get a thin/empty site -> low confidence downstream
        text = "" if RNG.random() < 0.10 else _COPY[sector]
        cands.append(dict(
            name=f"Biz {i+1:02d} ({sector.split('_')[0]})",
            sector=sector,
            distance_km=float(np.clip(RNG.exponential(4.0), 0.3, 18)),
            company_age=int(np.clip(RNG.normal(14, 9), 1, 60)),
            size_band=str(RNG.choice(sizes, p=[0.45, 0.35, 0.15, 0.05])),
            accounts_dormant=bool(RNG.random() < 0.05),
            website_text=text,
            warm_intro=None,
        ))
    return cands


def run(mode="stub"):
    cfg = ss.load_config()
    conn = db.connect(); db.init_db(conn)
    model = sf.DEFAULT_MODEL if mode == "llm" else "stub-keyword-v1"

    cands = make_candidates()
    biz_records, score_rows = [], []
    for rec in cands:
        rec["business_id"] = db.business_key(rec)
        flat = sf.enrich(rec, mode=mode)                     # step 2
        r = ss.score_business(flat, cfg)                     # step 1
        top = max(r["contrib"], key=r["contrib"].get)

        biz_records.append(rec)
        db.upsert_soft_features(conn, rec["business_id"], flat["_soft_derived"], model)
        score_rows.append(dict(
            business_id=rec["business_id"], fit_score=r["fit_score"],
            top_driver=top.replace("_", " "),
            contrib=r["contrib"], tier=ss.recommend_tier(rec["sector"], cfg),
            flags=ss.flags_for(flat, cfg)))

    db.upsert_businesses(conn, biz_records)                  # step 3
    db.save_scores(conn, score_rows, config_version="v1")

    # seed a couple of human statuses so the walled layer is visibly non-empty
    q = db.get_queue(conn)
    if len(q) >= 2:
        db.set_status(conn, q.iloc[0].business_id, "to-approach",
                      owner="owner", notes="Warm path via board — call first.")
        db.set_status(conn, q.iloc[3].business_id, "contacted",
                      owner="Tom", notes="Emailed 12 Sep, awaiting reply.")
    return conn


if __name__ == "__main__":
    mode = "llm" if "--llm" in sys.argv else "stub"
    print("=" * 74)
    print(f"SPONSOR PIPELINE  —  build the store end to end  (soft mode: {mode})")
    print("=" * 74)
    if mode == "llm" and not __import__("os").environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY — using offline stub for soft features.\n")
        mode = "stub"

    conn = run(mode)
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
    # re-run the score writer only (simulating a data refresh)
    rows = [dict(business_id=r["business_id"], fit_score=r["fit_score"],
                 top_driver=r["top_driver"], contrib={}, tier=r["tier"],
                 flags=r["flags"])
            for _, r in db.get_queue(conn).iterrows()]
    db.save_scores(conn, rows)
    after = db.get_queue(conn)[["business_id", "status", "notes"]]
    after = after[after.status != "new"]
    kept = before.merge(after, on=["business_id", "status", "notes"])
    print(f"human rows before refresh: {len(before)}  |  survived refresh: {len(kept)}")
    print("outreach_status is untouched by re-scoring — exactly the point.")
