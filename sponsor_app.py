#!/usr/bin/env python3
"""
sponsor_app.py  —  Dumbarton FC sponsor action queue (Streamlit UI)
===================================================================
Reads the SQLite store (sponsor.db) and shows the ranked prospect queue: each
business with its fit score, recommended tier, flags, and — expanded — the full
auditable "why" (every feature's point contribution + the LLM's three
justifications with their confidence).

Unlike the recruitment app, this one WRITES: a human works the queue top-down and
sets each prospect's status (to-approach / contacted / in-talks / won / dead) with
a note. Those writes go ONLY to outreach_status — the walled human table — so a
data refresh or re-score never touches them. The tool proposes; a person owns
every contact and every status change.

If no real store is present (or empty), it builds a small clearly-labelled DEMO
store so the UI renders immediately — nothing below is a real business.

Persistence note: SQLite is right for local / single-user use. On a shared
deployment (Streamlit Cloud's filesystem is ephemeral), point DUMBARTON_SPONSOR_DB
at a hosted DB so status edits survive restarts.

Run:  streamlit run sponsor_app.py
"""
import os
import json
import sqlite3
import tempfile
from datetime import datetime, timezone
import pandas as pd
import streamlit as st

DB_PATH = os.environ.get("DUMBARTON_SPONSOR_DB", "sponsor.db")
DEMO_PATH = os.path.join(tempfile.gettempdir(), "sponsor_demo.db")
STATUSES = ("new", "to-approach", "contacted", "in-talks", "won", "dead")


# ---------------------------------------------------------------------------
# data access (no st.* here, so it's testable without a browser)
# ---------------------------------------------------------------------------
def db_has_scores(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM business_scores").fetchone()[0]
        conn.close()
        return n > 0
    except Exception:
        return False


def build_demo_db(path: str):
    """A tiny, clearly-labelled demo store so the UI renders before real data."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        DROP TABLE IF EXISTS businesses; DROP TABLE IF EXISTS soft_features;
        DROP TABLE IF EXISTS business_scores; DROP TABLE IF EXISTS outreach_status;
        CREATE TABLE businesses (business_id TEXT PRIMARY KEY, name TEXT, sector TEXT,
          distance_km REAL, company_age INTEGER, size_band TEXT, accounts_dormant INTEGER,
          website_text TEXT, warm_intro TEXT, source TEXT, fetched_at TEXT);
        CREATE TABLE soft_features (business_id TEXT, feature TEXT, value REAL,
          confidence REAL, justification TEXT, model TEXT, derived_at TEXT,
          PRIMARY KEY (business_id, feature));
        CREATE TABLE business_scores (business_id TEXT PRIMARY KEY, fit_score REAL,
          top_driver TEXT, contrib_json TEXT, tier TEXT, flags TEXT,
          config_version TEXT, computed_at TEXT);
        CREATE TABLE outreach_status (business_id TEXT PRIMARY KEY, status TEXT,
          owner TEXT, notes TEXT, updated_at TEXT);
    """)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    demo = [  # id, name, sector, dist, tier, fit, flags, contrib
        ("d1", "DEMO — Marbill-style Coaches", "transport_coaches_haulage", 1.2,
         "B2B — programme, website, training-wear", 88.0, "WARM PATH via board",
         {"community_affinity": 21.0, "proximity": 16.7, "sector_propensity": 13.5,
          "brand_fit": 11.4, "company_stability": 11.0, "csr_signal": 8.4, "ability_to_pay": 6.0}),
        ("d2", "DEMO — Local Funeralcare", "funeral_directors", 2.6,
         "Community — programme + community-programme naming", 75.0, "micro — modest budget",
         {"community_affinity": 20.0, "proximity": 13.0, "sector_propensity": 10.5,
          "brand_fit": 10.7, "company_stability": 10.6, "csr_signal": 6.5, "ability_to_pay": 3.7}),
        ("d3", "DEMO — High St Independent", "independent_retail", 1.3,
         "Consumer — matchday, hospitality, visible board", 71.0, "-",
         {"community_affinity": 17.0, "proximity": 16.3, "sector_propensity": 9.0,
          "brand_fit": 9.8, "company_stability": 9.0, "csr_signal": 5.9, "ability_to_pay": 4.0}),
        ("d4", "DEMO — Town Physio", "health_fitness_physio_dental", 4.2,
         "Community — bundle the 4G pitch (pitch-time/naming)", 53.0, "no website found",
         {"community_affinity": 11.0, "proximity": 9.9, "sector_propensity": 9.75,
          "brand_fit": 6.0, "company_stability": 7.0, "csr_signal": 5.0, "ability_to_pay": 3.5}),
        ("d5", "DEMO — National MegaMart", "national_chain", 5.5,
         "Low priority — decisions likely not local", 45.0, "national chain — not local",
         {"community_affinity": 5.5, "proximity": 8.0, "sector_propensity": 3.0,
          "brand_fit": 3.0, "company_stability": 11.0, "csr_signal": 4.5, "ability_to_pay": 10.0}),
        ("d6", "DEMO — Online-Only Retailer", "online_ecommerce", 7.0,
         "Low priority — no local-visibility motive", 34.0, "online-only",
         {"community_affinity": 4.2, "proximity": 6.0, "sector_propensity": 3.75,
          "brand_fit": 2.5, "company_stability": 6.0, "csr_signal": 2.8, "ability_to_pay": 8.5}),
    ]
    for bid, name, sector, dist, tier, fit, flags, contrib in demo:
        top = max(contrib, key=contrib.get).replace("_", " ")
        conn.execute("INSERT INTO businesses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (bid, name, sector, dist, 20, "small", 0,
                      "(demo website copy)", "board — known sponsor" if bid == "d1" else None,
                      "demo", now))
        conn.execute("INSERT INTO business_scores VALUES (?,?,?,?,?,?,?,?)",
                     (bid, fit, top, json.dumps(contrib), tier, flags, "demo", now))
        for f, v, c, j in (
            ("community_affinity", contrib["community_affinity"] / 22, 0.9, "demo justification — local signals in copy"),
            ("brand_fit", contrib["brand_fit"] / 12, 0.85, "demo justification — town-facing business"),
            ("csr_signal", contrib["csr_signal"] / 10, 0.8, "demo justification — community support mentioned")):
            conn.execute("INSERT INTO soft_features VALUES (?,?,?,?,?,?,?)",
                         (bid, f, round(v, 2), c, j, "demo", now))
    conn.execute("INSERT INTO outreach_status VALUES (?,?,?,?,?)",
                 ("d1", "to-approach", "owner", "Warm path via board — call first.", now))
    conn.commit(); conn.close()


def load_queue(path: str, _mtime: float) -> pd.DataFrame:
    conn = sqlite3.connect(path)
    q = pd.read_sql(
        "SELECT s.business_id, b.name, b.sector, s.fit_score, b.distance_km, "
        "s.top_driver, s.tier, s.flags, b.warm_intro, "
        "COALESCE(o.status,'new') AS status, o.owner, o.notes, o.updated_at "
        "FROM business_scores s JOIN businesses b USING(business_id) "
        "LEFT JOIN outreach_status o USING(business_id) "
        "ORDER BY s.fit_score DESC", conn)
    conn.close()
    return q


def load_detail(path: str, _mtime: float, bid: str):
    conn = sqlite3.connect(path)
    b = pd.read_sql("SELECT * FROM businesses WHERE business_id=?", conn, params=(bid,))
    sf = pd.read_sql("SELECT feature,value,confidence,justification,model "
                     "FROM soft_features WHERE business_id=?", conn, params=(bid,))
    sc = pd.read_sql("SELECT contrib_json FROM business_scores WHERE business_id=?",
                     conn, params=(bid,))
    conn.close()
    contrib = json.loads(sc.contrib_json.iloc[0]) if len(sc) else {}
    return (b.iloc[0].to_dict() if len(b) else {}), sf, contrib


def write_status(path: str, bid: str, status: str, owner: str, notes: str):
    """The ONLY writer — touches outreach_status alone."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO outreach_status (business_id,status,owner,notes,updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
        "status=excluded.status, owner=excluded.owner, notes=excluded.notes, "
        "updated_at=excluded.updated_at", (bid, status, owner, notes, now))
    conn.commit(); conn.close()


_load = st.cache_data(load_queue)
_detail = st.cache_data(load_detail)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Dumbarton FC — sponsor queue", layout="wide")
    st.title("Dumbarton FC — sponsor action queue")
    st.caption("Local businesses ranked by sponsorship fit — community affinity "
               "over reach. A prioritisation aid: a person owns every contact.")

    demo = not db_has_scores(DB_PATH)
    if demo:
        build_demo_db(DEMO_PATH)
        path = DEMO_PATH
        st.warning("Showing **demo data** — run `sponsor_pipeline.py` to build a "
                   "real `sponsor.db`. Nothing below is a real business.", icon="⚠️")
    else:
        path = DB_PATH

    mtime = os.path.getmtime(path)
    q = _load(path, mtime)

    with st.sidebar:
        st.header("Filters")
        sectors = sorted(q.sector.dropna().unique())
        pick = st.multiselect("Sector", sectors, default=sectors)
        min_fit = st.slider("Minimum fit score", 0, 100, 0)
        pick_status = st.multiselect("Status", STATUSES, default=list(STATUSES))
        query = st.text_input("Search name").strip().lower()
        st.divider()
        st.caption("Sector fit is the hand-authored table (v1). The learned "
                   "lookalike engine replaces it next — same slot, real data.")

    view = q[q.sector.isin(pick) & (q.fit_score >= min_fit) & q.status.isin(pick_status)]
    if query:
        view = view[view.name.str.lower().str.contains(query)]

    won = (q.status == "won").sum()
    live = q.status.isin(["to-approach", "contacted", "in-talks"]).sum()
    m1, m2, m3 = st.columns(3)
    m1.metric("Prospects scored", len(q))
    m2.metric("In the pipeline", int(live))
    m3.metric("Won", int(won))

    tab_queue, tab_inspect = st.tabs(["Action queue", "Inspect a prospect"])

    with tab_queue:
        st.subheader(f"{len(view)} prospects")
        st.dataframe(
            view[["name", "sector", "fit_score", "distance_km", "tier", "status", "flags"]],
            hide_index=True, use_container_width=True,
            column_config={
                "name": "Business", "sector": "Sector",
                "fit_score": st.column_config.ProgressColumn(
                    "Fit", min_value=0, max_value=100, format="%.0f"),
                "distance_km": st.column_config.NumberColumn("km", format="%.1f"),
                "tier": "Suggested tier", "status": "Status", "flags": "Flags"})

    with tab_inspect:
        if not len(view):
            st.info("No prospects match the current filters.")
            return
        who = st.selectbox("Prospect", view.name.tolist())
        row = view[view.name == who].iloc[0]
        bid = row.business_id
        biz, sf, contrib = _detail(path, mtime, bid)

        c1, c2, c3 = st.columns(3)
        c1.metric("Fit score", f"{row.fit_score:.0f}")
        c2.metric("Distance", f"{row.distance_km:.1f} km")
        c3.metric("Top driver", row.top_driver)
        st.caption(f"Suggested tier: **{row.tier}**")
        if isinstance(row.warm_intro, str) and row.warm_intro.strip():
            st.success(f"Warm path: {row.warm_intro}")
        if row.flags and row.flags != "-":
            st.info(f"Flags: {row.flags}")

        st.markdown("**Why this score** — every feature's points (auditable):")
        if contrib:
            cdf = (pd.DataFrame({"feature": list(contrib), "points": list(contrib.values())})
                   .sort_values("points", ascending=False))
            st.bar_chart(cdf.set_index("feature")["points"])

        st.markdown("**LLM soft-feature reads** — value, confidence & the justification "
                    "(each cites a fact from the business's own text):")
        if len(sf):
            st.dataframe(
                sf.rename(columns={"feature": "Feature", "value": "Value",
                                   "confidence": "Confidence", "justification": "Justification",
                                   "model": "Model"}),
                hide_index=True, use_container_width=True,
                column_config={
                    "Value": st.column_config.NumberColumn(format="%.2f"),
                    "Confidence": st.column_config.ProgressColumn(
                        "Confidence", min_value=0.0, max_value=1.0, format="%.2f")})

        st.divider()
        st.markdown("**Work this prospect** — updates the human layer only:")
        cur_status = row.status if row.status in STATUSES else "new"
        a, b_ = st.columns(2)
        new_status = a.selectbox("Status", STATUSES, index=STATUSES.index(cur_status))
        owner = b_.text_input("Owner", value=row.owner or "")
        note = st.text_area("Note", value=row.notes or "")
        if st.button("Save status", type="primary"):
            write_status(path, bid, new_status, owner, note)
            _load.clear(); _detail.clear()
            st.success("Saved to the outreach layer.")
            st.rerun()


if __name__ == "__main__":
    main()
