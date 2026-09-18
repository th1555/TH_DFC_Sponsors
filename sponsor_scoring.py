#!/usr/bin/env python3
"""
Sponsor scoring core  —  Dumbarton FC Sponsor Intelligence Tool (the engine)
============================================================================
The auditable heart of the sponsor tool. Turns a business record into a
transparent 0-100 FIT SCORE by combining explicit features with tunable weights
(sponsor_config.yaml) — never "ask an LLM for 87/100 from nothing". Every score
decomposes into per-feature contributions, so the HITL queue can always show
*why* a prospect scored what it did.

Design philosophy, encoded in the weights: Dumbarton sells LOCAL AFFINITY, not
reach. So community affinity + proximity dominate; raw company size is light.

Feature split (see spec):
  DETERMINISTIC (plain code / API fields):
     proximity          exp-decay on distance from the ground
     sector_propensity  lookup table  (v2: replaced by the lookalike engine)
     company_stability  trust from age, penalty for dormant accounts
     ability_to_pay     size band, deliberately LIGHT-weighted
  LLM SOFT FEATURES (derived by the bounded LLM layer, stubbed here):
     community_affinity, brand_fit, csr_signal   — each 0..1 * a confidence,
     pulled toward neutral when the model is unsure (anti-swing guardrail).

This runs on SYNTHETIC businesses with a known shape so you can watch the engine
rank (coach firms & trades rise, national chains & online sink) and tune weights
BEFORE any Places / Companies House key exists. The `score_business()` function
is the clean seam: feed it a real enriched record and it scores identically.

Run:  python sponsor_scoring.py
Deps: numpy, pandas, matplotlib, pyyaml   (+ sponsor_config.yaml beside it)
"""
from __future__ import annotations
import math
import yaml
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RNG = np.random.default_rng(11)
CONFIG_PATH = "sponsor_config.yaml"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def load_config(path=CONFIG_PATH) -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    total = sum(cfg["weights"].values())
    if round(total) != 100:
        raise ValueError(f"weights must sum to 100, got {total}")
    return cfg


# ---------------------------------------------------------------------------
# deterministic feature functions  (all return 0..1)
# ---------------------------------------------------------------------------
def f_proximity(distance_km, cfg):
    return math.exp(-max(0.0, distance_km) / cfg["proximity"]["decay_km"])


def f_sector(sector, cfg):
    tbl = cfg["sector_propensity"]
    return float(tbl.get(sector, tbl["unknown"]))


def f_stability(age_years, dormant, cfg):
    s = 1.0 - math.exp(-max(0.0, age_years) / cfg["stability"]["age_scale_years"])
    if dormant:
        s *= cfg["stability"]["dormant_penalty"]
    return s


def f_ability(size_band, cfg):
    return float(cfg["ability_to_pay"].get(size_band, cfg["ability_to_pay"]["unknown"]))


def soft(value, confidence, cfg):
    """LLM soft feature with the anti-swing guardrail: low confidence pulls the
    read toward neutral instead of letting it move the score."""
    neutral = cfg["soft_features"]["neutral"]
    return value * confidence + neutral * (1.0 - confidence)


# ---------------------------------------------------------------------------
# THE CLEAN SEAM — score one enriched business record
# ---------------------------------------------------------------------------
def score_business(b: dict, cfg: dict) -> dict:
    """b is one enriched record (the shape Places+Companies House+LLM produce).
    Returns the fit score plus every feature's raw value and its point
    contribution, so the queue can render an auditable 'why'."""
    w = cfg["weights"]
    feats = {
        "community_affinity": soft(b["community_affinity"], b["community_conf"], cfg),
        "proximity":          f_proximity(b["distance_km"], cfg),
        "sector_propensity":  f_sector(b["sector"], cfg),
        "brand_fit":          soft(b["brand_fit"], b["brand_conf"], cfg),
        "company_stability":  f_stability(b["company_age"], b["accounts_dormant"], cfg),
        "csr_signal":         soft(b["csr_signal"], b["csr_conf"], cfg),
        "ability_to_pay":     f_ability(b["size_band"], cfg),
    }
    contrib = {k: feats[k] * w[k] for k in w}          # points out of each weight
    fit = round(sum(contrib.values()), 1)              # 0..100
    return {"fit_score": fit, "features": feats, "contrib": contrib}


def recommend_tier(sector, cfg):
    return cfg["tier_map"].get(sector, cfg["tier_map"]["unknown"])


def flags_for(b, cfg):
    fl = []
    if b["accounts_dormant"]:
        fl.append("dormant accounts — check still trading")
    if b["sector"] == "national_chain":
        fl.append("national chain — decisions likely not local")
    if b["sector"] == "online_ecommerce":
        fl.append("online-only — no local-visibility motive")
    if b["size_band"] == "micro":
        fl.append("micro business — modest budget")
    warm = b.get("warm_intro")
    if isinstance(warm, str) and warm.strip():        # None/NaN are not warm paths
        fl.append(f"WARM PATH via {warm}")
    return "; ".join(fl) or "-"


# ---------------------------------------------------------------------------
# synthetic businesses — the shape the real enrichment pipeline will produce
# ---------------------------------------------------------------------------
SECTORS = list({
    "transport_coaches_haulage", "building_construction_trades",
    "pubs_bars_hospitality", "motor_dealers_garages",
    "local_professional_services", "funeral_directors",
    "food_takeaway_restaurants", "utilities_renewables_heating",
    "health_fitness_physio_dental", "independent_retail",
    "national_chain", "online_ecommerce", "public_sector_charity",
})

# a few hand-seeded, legible archetypes so the demo READS clearly, then noise
SEEDED = [
    # name, sector, distance_km, age, size, dormant, community_truth, warm_intro
    ("Marbill Coaches",        "transport_coaches_haulage",   1.2, 38, "small",  False, 0.92, "Board — known sponsor"),
    ("Leven Valley Joinery",   "building_construction_trades",2.1, 15, "micro",  False, 0.80, None),
    ("The Anchor Inn",         "pubs_bars_hospitality",       0.6, 22, "small",  False, 0.78, None),
    ("Dumbarton Motors",       "motor_dealers_garages",       1.8, 27, "small",  False, 0.74, None),
    ("Rock City Accountants",  "local_professional_services", 3.0, 11, "small",  False, 0.62, "Owner network"),
    ("Bonhill Funeralcare",    "funeral_directors",           2.6, 40, "micro",  False, 0.70, None),
    ("Riverside Physio",       "health_fitness_physio_dental",4.2, 6,  "micro",  False, 0.58, None),
    ("West End Chippy",        "food_takeaway_restaurants",   0.9, 9,  "micro",  False, 0.55, None),
    ("Clydebank MegaMart",     "national_chain",              5.5, 30, "large",  False, 0.20, None),
    ("ByteBargains Online",    "online_ecommerce",            7.0, 4,  "medium", False, 0.15, None),
    ("Dormant Holdings Ltd",   "independent_retail",          3.3, 12, "small",  True,  0.30, None),
]


def make_businesses(n_random=25) -> pd.DataFrame:
    rows = []
    def add(name, sector, dist, age, size, dormant, comm_truth, warm):
        # LLM soft features: a 'true' value + a confidence the model reports.
        # community_affinity is seeded; brand_fit / csr correlate loosely with it.
        comm_conf = float(np.clip(RNG.normal(0.8, 0.12), 0.4, 0.99))
        brand = float(np.clip(comm_truth + RNG.normal(0, 0.12), 0, 1))
        csr = float(np.clip(comm_truth * 0.8 + RNG.normal(0.05, 0.15), 0, 1))
        rows.append(dict(
            name=name, sector=sector, distance_km=dist, company_age=age,
            size_band=size, accounts_dormant=dormant,
            community_affinity=comm_truth, community_conf=comm_conf,
            brand_fit=brand,                brand_conf=float(np.clip(RNG.normal(0.75, 0.14), 0.4, 0.99)),
            csr_signal=csr,                 csr_conf=float(np.clip(RNG.normal(0.7, 0.15), 0.35, 0.99)),
            warm_intro=warm,
        ))
    for s in SEEDED:
        add(*s)
    # random businesses to fill out a realistic candidate pool
    sizes = ["micro", "small", "medium", "large"]
    for i in range(n_random):
        sector = str(RNG.choice(SECTORS))
        local = sector not in ("national_chain", "online_ecommerce", "public_sector_charity")
        comm = float(np.clip(RNG.normal(0.6 if local else 0.25, 0.15), 0.02, 0.98))
        add(f"Biz {i+1:02d} ({sector.split('_')[0]})", sector,
            float(np.clip(RNG.exponential(4.0), 0.3, 18)),
            int(np.clip(RNG.normal(14, 9), 1, 60)),
            str(RNG.choice(sizes, p=[0.45, 0.35, 0.15, 0.05])),
            bool(RNG.random() < 0.05), comm, None)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# build the ranked queue
# ---------------------------------------------------------------------------
def build_queue(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    recs = []
    for _, b in df.iterrows():
        r = score_business(b.to_dict(), cfg)
        # the single biggest positive driver, for a one-glance reason
        top = max(r["contrib"], key=r["contrib"].get)
        recs.append(dict(
            business=b["name"], sector=b["sector"], fit_score=r["fit_score"],
            distance_km=round(b["distance_km"], 1),
            top_driver=top.replace("_", " "),
            tier=recommend_tier(b["sector"], cfg),
            flags=flags_for(b.to_dict(), cfg),
            _contrib=r["contrib"],
        ))
    q = pd.DataFrame(recs).sort_values("fit_score", ascending=False).reset_index(drop=True)
    return q


def explain(row) -> str:
    """The audit trail for one prospect — every feature's point contribution."""
    c = row["_contrib"]
    parts = sorted(c.items(), key=lambda kv: kv[1], reverse=True)
    return "  ".join(f"{k.replace('_',' ')}={v:.1f}" for k, v in parts)


def plot_queue(q, path="sponsor_queue.png", top=15):
    d = q.head(top).iloc[::-1]
    palette = {  # colour by whether it's a local-affinity sector or not
        True: "#c8102e", False: "#9e9e9e"}
    local = [s not in ("national_chain", "online_ecommerce", "public_sector_charity")
             for s in d.sector]
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, ax = plt.subplots(figsize=(9.5, 7))
    y = range(len(d))
    ax.barh(list(y), d.fit_score, color=[palette[l] for l in local])
    for yi, (_, r) in zip(y, d.iterrows()):
        ax.text(r.fit_score + 0.6, yi, f"{r.tier.split(' — ')[0]}  ·  {r.top_driver}",
                va="center", fontsize=7.5)
    ax.set_yticks(list(y)); ax.set_yticklabels(d.business, fontsize=8)
    ax.set_xlabel("Fit score  (0–100, weighted composite)")
    ax.set_xlim(0, 100)
    ax.set_title("Dumbarton sponsor prospects — ranked by local-affinity fit",
                 fontsize=13, fontweight="bold")
    ax.text(0.5, -0.5, "red = local-affinity sector   ·   grey = national/online (down-weighted)",
            transform=ax.transData, fontsize=7.5, color="#555")
    plt.tight_layout(); plt.savefig(path, dpi=130)
    print(f"\nsaved queue chart -> {path}")


if __name__ == "__main__":
    print("=" * 74)
    print("SPONSOR SCORING CORE  —  ranked prospect queue (synthetic demo)")
    print("=" * 74)
    cfg = load_config()
    print("weights (sum=100):", {k: v for k, v in cfg["weights"].items()})

    df = make_businesses()
    q = build_queue(df, cfg)
    print(f"\nscored {len(q)} candidate businesses "
          f"({(df.sector.isin(['national_chain','online_ecommerce','public_sector_charity'])).sum()} "
          f"national/online/public, down-weighted)\n")

    cols = ["business", "sector", "fit_score", "distance_km", "top_driver", "flags"]
    print("RANKED PROSPECT QUEUE (top 15) — the human works this top-down:\n")
    print(q[cols].head(15).to_string(index=False))

    print("\n--- audit trail for the #1 prospect (every feature's points) ---")
    top1 = q.iloc[0]
    print(f"{top1.business}  ->  fit {top1.fit_score}")
    print(" ", explain(top1))
    print(f"  recommended: {top1.tier}")

    plot_queue(q)
    print("\nRead-out: local coach firms, trades and pubs rise on community +")
    print("proximity; the national chain and online-only retailer sink despite")
    print("size, exactly as the affinity reframing intends. Every row is auditable,")
    print("and a human owns every contact — the tool only prioritises + prepares.")
