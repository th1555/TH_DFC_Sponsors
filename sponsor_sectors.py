#!/usr/bin/env python3
"""
sponsor_sectors.py  —  sector ratings counted from real club sponsor lists
==========================================================================
Replaces the hand-set sector table with a count: for each kind of business,
how common it is among the sponsors of clubs like Dumbarton, compared with how
common it is among Scottish businesses generally.

1. CLASSIFY each distinct sponsor found by club_sponsors.py (clubs in the
   configured tiers), strongest evidence first:
     chain     on the national chains list (config `chains`)
     name      the name says what it does ("... Coaches", "... Print")
     company   a confident Companies House name match -> its SIC codes
     ai        the AI model's reading of the name (labelled as such)
     unknown   none of the above: left out of the count
2. COUNT sponsors per sector: n_s of N.
3. BASE: each sector's share of Scottish businesses, p_s (ONS business
   register counts by industry, via build_business_base.py).
4. RATE, with caution for thin evidence (like the scouting tool's thin leagues):
     smoothed share  q_s = (n_s + a * p_s) / (N + a)        a = prior_sponsors
     lift            L_s = q_s / p_s       (1 = as common among sponsors as in business)
     rating          r_s = L_s / (1 + L_s) (0.5 = average, 0.75 = 3x, 0.9 = 9x)
   A sector with few sponsors is pulled towards 0.5 instead of swinging.

Sectors SIC can't measure stay hand-set and say so: national chains (a chain is
about where decisions are made, not what the business does) and "unknown".

Outputs sector_ratings.csv (one row per sector with every number used) and,
when config sector_ratings.use_learned is on and there's enough evidence,
sponsor_scoring.load_config() uses these ratings in place of the hand-set ones.

    python sponsor_sectors.py                offline checks
    python sponsor_sectors.py --live         classify sponsors + write sector_ratings.csv
    python sponsor_sectors.py --live --no-ai no AI fallback for unclassified names
"""
from __future__ import annotations
import csv
import json
import os
import re
import sys
from collections import Counter

import data_checks as dc
import sic_sectors
from club_sponsors import norm

RATINGS_FILE = "sector_ratings.csv"
BASE_FILE = "business_base_scotland.csv"
UNMEASURED = {"national_chain", "unknown"}


# ---------------------------------------------------------------------------
# 1. CLASSIFY
# ---------------------------------------------------------------------------
def classify_one(name, domain, cfg, ch_lookup=None) -> tuple[str, str, str | None]:
    """(sector, method, detail). ch_lookup(name) -> (company_number, sic_codes, score) | None."""
    why = dc.chain_reason(name, f"https://{domain}" if domain else None, cfg)
    if why:
        return "national_chain", "chain", why
    s = dc.sector_from_name(name)
    if s:
        return s, "name", None
    if ch_lookup:
        hit = ch_lookup(name)
        if hit and hit[2] >= 0.9 and hit[1]:
            sec = sic_sectors.map_sectors(hit[1])
            if sec != "unknown":
                return sec, "company", f"{hit[0]} (SIC {', '.join(hit[1])})"
    return "unknown", "unknown", None


def ch_lookup_api(name):
    """Best active Companies House match by name alone (sponsors are all over
    Scotland, so no place check); only a near-exact name counts (>= 0.9)."""
    import ingest_companies_house as ch
    try:
        items = ch.search_companies_api(name, n=10)
    except Exception:
        return None
    best = None
    for it in items:
        if str(it.get("company_status") or "").lower() != "active":
            continue
        sc = ch.name_score(name, it.get("title", ""))
        if best is None or sc > best[0]:
            best = (sc, it)
    if not best or best[0] < 0.9:
        return None
    prof = ch.get_profile_api(best[1]["company_number"])
    return best[1]["company_number"], [str(c) for c in prof.get("sic_codes") or []], best[0]


AI_SYSTEM = ("You classify businesses that sponsor Scottish football clubs into sectors, from the business NAME "
             "only. Answer 'unknown' unless the name makes the kind of business clear. Return ONLY JSON: "
             "{\"results\": [{\"name\": str, \"sector\": str}]} using exactly these sector keys: ")


def ai_classify(names, sectors, api_key=None, model=None) -> dict:
    """Batched name-only classification for what the rules couldn't place."""
    import requests, time
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or os.environ.get("SPONSOR_LLM_MODEL", "claude-haiku-5-5")
    out = {}
    for i in range(0, len(names), 60):
        chunk = names[i:i + 60]
        for attempt in range(4):
            try:
                r = requests.post("https://api.anthropic.com/v1/messages",
                                  headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                           "content-type": "application/json"},
                                  json={"model": model, "max_tokens": 4000,
                                        "system": AI_SYSTEM + ", ".join(sectors),
                                        "messages": [{"role": "user", "content": "\n".join(chunk)}]}, timeout=90)
                if r.status_code in (429, 500, 502, 503, 529):
                    time.sleep(3 * 2 ** attempt); continue
                r.raise_for_status()
                text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
                text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
                for row in json.loads(text).get("results", []):
                    if row.get("sector") in sectors:
                        out[row["name"]] = row["sector"]
                break
            except Exception:
                time.sleep(3 * 2 ** attempt)
    return out


def classify_all(sponsors, cfg, ch_lookup=None, use_ai=False, cache=None):
    """sponsors: [(name, domain)] distinct. Returns {norm(name): dict(...)}"""
    cache = cache or {}
    out, pending = {}, []
    for name, domain in sponsors:
        k = norm(name)
        if k in out:
            continue
        if k in cache:
            out[k] = cache[k]
            continue
        sec, method, detail = classify_one(name, domain, cfg, ch_lookup)
        out[k] = dict(name=name, sector=sec, method=method, detail=detail)
        if sec == "unknown":
            pending.append(name)
    if use_ai and pending:
        keys = [s for s in cfg["sector_propensity"] if s not in ("unknown",)]
        got = ai_classify(pending, keys)
        for name, sec in got.items():
            k = norm(name)
            if k in out and out[k]["sector"] == "unknown":
                out[k].update(sector=sec, method="ai", detail="from the name, by the AI model")
    return out


# ---------------------------------------------------------------------------
# 3. BASE and 4. RATE
# ---------------------------------------------------------------------------
def load_base(path=BASE_FILE) -> dict | None:
    """sector -> share of Scottish businesses (measured sectors only, sums to 1)."""
    if not os.path.exists(path):
        return None
    counts = Counter()
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            sec = sic_sectors.sic_to_sector(str(r["sic"]).ljust(5, "0"))
            if sec not in UNMEASURED:
                counts[sec] += float(r["count"])
    tot = sum(counts.values())
    return {s: c / tot for s, c in counts.items()} if tot else None


def rate(classified: dict, base: dict, cfg: dict) -> list[dict]:
    sr = cfg.get("sector_ratings") or {}
    a = float(sr.get("prior_sponsors", 30))
    hand = cfg["sector_propensity"]
    n = Counter(v["sector"] for v in classified.values() if v["sector"] not in UNMEASURED)
    N = sum(n.values())
    rows = []
    for s in hand:
        if s in UNMEASURED or s not in base:
            rows.append(dict(sector=s, method="hand-set", sponsors=int(n.get(s, 0)), sponsor_share=None,
                             business_share=None, lift=None, rating=float(hand[s]), hand_set=float(hand[s])))
            continue
        p = base[s]
        q = (n.get(s, 0) + a * p) / (N + a)
        lift = q / p
        rows.append(dict(sector=s, method="counted", sponsors=int(n.get(s, 0)),
                         sponsor_share=round(n.get(s, 0) / N, 4) if N else 0.0, business_share=round(p, 4),
                         lift=round(lift, 3), rating=round(lift / (1 + lift), 3), hand_set=float(hand[s])))
    return rows


def write_ratings(rows, meta, path=RATINGS_FILE):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# {json.dumps(meta)}\n")
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def read_ratings(path=RATINGS_FILE):
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8") as f:
        first = f.readline()
        meta = json.loads(first[1:]) if first.startswith("#") else {}
        if not first.startswith("#"):
            f.seek(0)
        rows = list(csv.DictReader(f))
    return rows, meta


def apply_learned(cfg: dict) -> dict:
    """Called by sponsor_scoring.load_config(): use counted ratings where we
    have them (and enough evidence overall), hand-set elsewhere. Records which
    is which in cfg['sector_method'] so the page can label each one."""
    sr = cfg.get("sector_ratings") or {}
    cfg["sector_method"] = {s: "hand-set" for s in cfg["sector_propensity"]}
    if not sr.get("use_learned"):
        return cfg
    rows, meta = read_ratings(sr.get("file", RATINGS_FILE))
    if not rows or (meta or {}).get("sponsors_counted", 0) < sr.get("min_sponsors", 150):
        return cfg
    cfg["sector_propensity_hand"] = dict(cfg["sector_propensity"])
    cfg["sector_evidence"] = {r["sector"]: r for r in rows}
    cfg["sector_evidence_meta"] = meta
    for r in rows:
        if r["method"] == "counted" and r["sector"] in cfg["sector_propensity"]:
            cfg["sector_propensity"][r["sector"]] = float(r["rating"])
            cfg["sector_method"][r["sector"]] = "counted"
    return cfg


def run_live(conn, cfg, use_ai=True, log=print):
    sr = cfg.get("sector_ratings") or {}
    lo, hi = sr.get("tiers", [3, 10])
    rows = conn.execute("SELECT DISTINCT sponsor_name, sponsor_domain FROM club_sponsors WHERE tier BETWEEN ? AND ?",
                        (lo, hi)).fetchall()
    conn.execute("CREATE TABLE IF NOT EXISTS sponsor_sectors (sponsor_key TEXT PRIMARY KEY, name TEXT, "
                 "sector TEXT, method TEXT, detail TEXT)")
    cache = {k: dict(name=n, sector=s, method=m, detail=d) for k, n, s, m, d in
             conn.execute("SELECT sponsor_key, name, sector, method, detail FROM sponsor_sectors")}
    have_ch = bool(os.environ.get("COMPANIES_HOUSE_API_KEY"))
    log(f"  classifying {len(rows)} distinct sponsors (clubs at tiers {lo}-{hi}); "
        f"Companies House {'on' if have_ch else 'off'}, AI {'on' if use_ai else 'off'}, {len(cache)} cached")
    classified = classify_all(rows, cfg, ch_lookup_api if have_ch else None, use_ai, cache)
    conn.executemany("INSERT OR REPLACE INTO sponsor_sectors VALUES (?,?,?,?,?)",
                     [(k, v["name"], v["sector"], v["method"], v["detail"]) for k, v in classified.items()])
    conn.commit()
    base = load_base()
    if not base:
        log(f"  no {BASE_FILE} yet (build it with build_business_base.py): sponsors classified, ratings not written")
        return None
    out = rate(classified, base, cfg)
    by = Counter(v["method"] for v in classified.values())
    meta = dict(sponsors_counted=sum(r["sponsors"] for r in out if r["method"] == "counted"),
                sponsors_total=len(classified), methods=dict(by), tiers=[lo, hi],
                prior_sponsors=sr.get("prior_sponsors", 30), base=BASE_FILE)
    write_ratings(out, meta)
    log(f"  wrote {RATINGS_FILE}: {meta['sponsors_counted']} sponsors counted across "
        f"{sum(1 for r in out if r['method'] == 'counted')} sectors; methods {dict(by)}")
    return out


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open("sponsor_config.yaml"))
    if "--live" in sys.argv:
        import sponsor_db as db
        conn = db.connect(); db.init_db(conn)
        run_live(conn, cfg, use_ai=("--no-ai" not in sys.argv) and bool(os.environ.get("ANTHROPIC_API_KEY")))
        sys.exit(0)

    print("sector ratings — offline checks\n" + "=" * 40)
    fake_ch = {"Algas": ("SC1", ["35220"], 1.0), "Ngage Technology": ("SC2", ["62020"], 0.95)}
    c = classify_all([("Arnold Clark", None), ("Rockside Coaches", None), ("JJR Print & Design", None),
                      ("Algas", "algas.example"), ("Ngage Technology", None), ("Mystery Ltd", None)],
                     cfg, ch_lookup=lambda n: fake_ch.get(n))
    got = {v["name"]: (v["sector"], v["method"]) for v in c.values()}
    assert got["Arnold Clark"] == ("national_chain", "chain")
    assert got["Rockside Coaches"] == ("transport_coaches_haulage", "name")
    assert got["JJR Print & Design"] == ("industrial_b2b_services", "name")
    assert got["Algas"] == ("industrial_b2b_services", "company")
    assert got["Mystery Ltd"] == ("unknown", "unknown")
    print("  chain, name, Companies House and unknown routes ✓")

    # rating maths: a sector 3x as common among sponsors as in business -> ~0.75
    base = {"transport_coaches_haulage": 0.02, "food_takeaway_restaurants": 0.10, "independent_retail": 0.88}
    classified = {f"t{i}": dict(sector="transport_coaches_haulage") for i in range(60)}
    classified.update({f"f{i}": dict(sector="food_takeaway_restaurants") for i in range(100)})
    classified.update({f"r{i}": dict(sector="independent_retail") for i in range(840)})
    rows = {r["sector"]: r for r in rate(classified, base, cfg)}
    t = rows["transport_coaches_haulage"]
    assert t["method"] == "counted" and 2.8 < t["lift"] < 3.0 and 0.73 < t["rating"] < 0.76, t
    assert abs(rows["food_takeaway_restaurants"]["rating"] - 0.5) < 0.01
    assert rows["national_chain"]["method"] == "hand-set"
    # thin evidence is pulled towards the middle: 2 of 20 sponsors when 2% of business
    thin = rate({**{f"t{i}": dict(sector="transport_coaches_haulage") for i in range(2)},
                 **{f"r{i}": dict(sector="independent_retail") for i in range(18)}}, base, cfg)
    th = next(r for r in thin if r["sector"] == "transport_coaches_haulage")
    assert 0.5 < th["rating"] < 0.75, th          # raw lift would be 5x (0.83); shrunk well below
    print(f"  3x as common among sponsors -> rating {t['rating']}; average -> 0.5; "
          f"thin evidence (2 of 20) -> {th['rating']} instead of 0.83 ✓")
