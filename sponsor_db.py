#!/usr/bin/env python3
"""
sponsor_db.py  —  the persistent store for the Dumbarton sponsor tool
=====================================================================
One SQLite file, same three principles as the recruitment store (db.py):
  * ACCUMULATE, don't overwrite — businesses keyed by a stable business_id and
    upserted, so re-running enrichment refreshes facts without losing history.
  * PROVENANCE — every scraped/derived row carries a source + timestamp (how
    stale is this? which model derived it?).
  * WALL OFF THE HUMAN LAYER — `outreach_status` is a separate table the scoring
    pipeline NEVER touches. Re-scoring the whole queue can't wipe a single note.

Table roles:
  businesses        the enriched record (Places + Companies House + website text)
  company_accounts  employees / turnover read from filed accounts, per company
  revenue_estimates the employee and revenue bands shown on the page, with working
                    (RECOMPUTABLE from company_accounts + the ONS ratio table)
  soft_features     the LLM's 3 judgments, one row each, WITH provenance
  business_scores   the deterministic fit score — RECOMPUTABLE (rebuilt on re-run)
  outreach_status   THE HUMAN WALL — status / owner / notes, written only by people

The score table is disposable (delete + rebuild); the human table is sacred.
That asymmetry is the design.
"""
from __future__ import annotations
import os
import re
import json
import sqlite3
from datetime import datetime, timezone
import pandas as pd

# Same env var the app reads, so pipeline + app agree on one file. In Colab, set
# it to a Google Drive path to persist the store (and the human notes) across runs.
DB_PATH = os.environ.get("DUMBARTON_SPONSOR_DB", "sponsor.db")
STATUSES = ("new", "to-approach", "contacted", "in-talks", "won", "dead")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def business_key(rec: dict) -> str:
    """Stable key: Companies House number if we have one, else a name slug.
    Real pipeline prefers company_number / Places place_id; slug is the fallback."""
    cn = str(rec.get("company_number") or "").strip()
    if cn:
        return f"ch:{cn}"
    slug = re.sub(r"[^a-z0-9]+", "-", str(rec.get("name", "")).lower()).strip("-")
    return f"nm:{slug}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
  business_id TEXT PRIMARY KEY, name TEXT, sector TEXT, distance_km REAL,
  company_age INTEGER, size_band TEXT, accounts_dormant INTEGER,
  website_text TEXT, warm_intro TEXT, source TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS soft_features (
  business_id TEXT, feature TEXT, value REAL, confidence REAL,
  justification TEXT, model TEXT, derived_at TEXT,
  PRIMARY KEY (business_id, feature)
);
CREATE TABLE IF NOT EXISTS business_scores (
  business_id TEXT PRIMARY KEY, fit_score REAL, top_driver TEXT,
  contrib_json TEXT, tier TEXT, flags TEXT, config_version TEXT, computed_at TEXT
);
CREATE TABLE IF NOT EXISTS company_accounts (
  company_number TEXT PRIMARY KEY, status TEXT, employees INTEGER, turnover INTEGER,
  period_end TEXT, filed_on TEXT, accounts_type TEXT, source_url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS revenue_estimates (
  business_id TEXT PRIMARY KEY, employees INTEGER, employees_band TEXT,
  employees_basis TEXT, employees_note TEXT, revenue_basis TEXT, revenue_band TEXT,
  revenue_value INTEGER, revenue_working TEXT, est_section TEXT, est_avg_turnover INTEGER,
  est_source TEXT, accounts_period_end TEXT, accounts_source TEXT,
  method_version TEXT, computed_at TEXT
);
CREATE TABLE IF NOT EXISTS outreach_status (
  business_id TEXT PRIMARY KEY, status TEXT, owner TEXT, notes TEXT, updated_at TEXT
);
"""


# Facts added after v1. ALTER TABLE keeps an existing store (and its human
# notes) intact instead of forcing a rebuild.
EXTRA_BUSINESS_COLS = {"company_number": "TEXT", "website": "TEXT",
                       "address": "TEXT", "postcode": "TEXT", "ch_flag": "TEXT",
                       # v2: local connections + extra signals
                       "connection_kind": "TEXT", "connection_note": "TEXT",
                       "connection_strength": "REAL", "directors_local": "INTEGER",
                       "directors_total": "INTEGER", "google_rating": "REAL",
                       "google_reviews": "INTEGER", "company_known": "INTEGER",
                       # v3: raw industry codes, for the revenue estimate
                       "sic_codes": "TEXT"}


def init_db(conn):
    # revenue_estimates is disposable (rebuilt every run): if an older layout is
    # there, drop it rather than migrate
    cols = {r[1] for r in conn.execute("PRAGMA table_info(revenue_estimates)")}
    if cols and "est_avg_turnover" not in cols:
        conn.execute("DROP TABLE revenue_estimates")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(businesses)")}
    for col, typ in EXTRA_BUSINESS_COLS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE businesses ADD COLUMN {col} {typ}")
    conn.commit()


# ---------------------------------------------------------------------------
# PIPELINE WRITERS  (facts + derivations + scores — never touch outreach_status)
# ---------------------------------------------------------------------------
def upsert_businesses(conn, records):
    cols = ["business_id", "name", "sector", "distance_km", "company_age", "size_band",
            "accounts_dormant", "website_text", "warm_intro", "source", "fetched_at",
            "company_number", "website", "address", "postcode", "ch_flag",
            "connection_kind", "connection_note", "connection_strength", "directors_local",
            "directors_total", "google_rating", "google_reviews", "company_known",
            "sic_codes"]
    rows = [(r["business_id"], r.get("name"), r.get("sector"),
             float(r.get("distance_km", 0) or 0), int(r.get("company_age", 0) or 0),
             r.get("size_band"), int(bool(r.get("accounts_dormant"))),
             r.get("website_text"), r.get("warm_intro"),
             r.get("source", "synthetic"), now(),
             r.get("company_number"), r.get("website"), r.get("address"),
             r.get("postcode"), r.get("ch_flag"),
             r.get("connection_kind"), r.get("connection_note"), r.get("connection_strength"),
             r.get("directors_local"), r.get("directors_total"), r.get("google_rating"),
             r.get("google_reviews"),
             int(bool(r.get("company_known", bool(r.get("company_number"))))),
             ",".join(str(c) for c in (r.get("sic_codes") or [])) or None) for r in records]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "business_id")
    conn.executemany(
        f"INSERT INTO businesses ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(business_id) DO UPDATE SET {updates}", rows)
    conn.commit()


def upsert_accounts(conn, rows):
    """Recorded figures from filed accounts (ingest_accounts.read_accounts rows)."""
    cols = ["company_number", "status", "employees", "turnover", "period_end",
            "filed_on", "accounts_type", "source_url", "fetched_at"]
    data = [tuple(r.get(c) for c in cols[:-1]) + (now(),) for r in rows if r.get("company_number")]
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols[1:])
    conn.executemany(
        f"INSERT INTO company_accounts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(company_number) DO UPDATE SET {updates}", data)
    conn.commit()


def save_estimates(conn, rows):
    """The bands shown on the page. Disposable: rebuilt from accounts + ratios."""
    cols = ["business_id", "employees", "employees_band", "employees_basis", "employees_note",
            "revenue_basis", "revenue_band", "revenue_value", "revenue_working", "est_section",
            "est_avg_turnover", "est_source", "accounts_period_end", "accounts_source",
            "method_version"]
    conn.execute("DELETE FROM revenue_estimates")
    conn.executemany(
        f"INSERT INTO revenue_estimates ({','.join(cols)},computed_at) "
        f"VALUES ({','.join('?' * (len(cols) + 1))})",
        [tuple(r.get(c) for c in cols) + (now(),) for r in rows])
    conn.commit()


def upsert_soft_features(conn, business_id, derived: dict, model: str):
    """derived is {feature: {value, confidence, justification}} from soft_features.py"""
    rows = [(business_id, f, float(d["value"]), float(d["confidence"]),
             d["justification"], model, now()) for f, d in derived.items()]
    conn.executemany(
        "INSERT INTO soft_features (business_id,feature,value,confidence,"
        "justification,model,derived_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(business_id,feature) DO UPDATE SET value=excluded.value, "
        "confidence=excluded.confidence, justification=excluded.justification, "
        "model=excluded.model, derived_at=excluded.derived_at", rows)
    conn.commit()


def save_scores(conn, score_rows, config_version="v1"):
    """RECOMPUTABLE: wipe and rebuild the whole score table. This is safe precisely
    because outreach_status is a different table we never touch here."""
    conn.execute("DELETE FROM business_scores")
    rows = [(r["business_id"], float(r["fit_score"]), r["top_driver"],
             json.dumps(r["contrib"]), r["tier"], r["flags"], config_version, now())
            for r in score_rows]
    conn.executemany("INSERT INTO business_scores VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()


# ---------------------------------------------------------------------------
# HUMAN WRITER  (the ONLY thing that writes outreach_status)
# ---------------------------------------------------------------------------
def set_status(conn, business_id, status, owner="", notes=""):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute(
        "INSERT INTO outreach_status (business_id,status,owner,notes,updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(business_id) DO UPDATE SET "
        "status=excluded.status, owner=excluded.owner, notes=excluded.notes, "
        "updated_at=excluded.updated_at",
        (business_id, status, owner, notes, now()))
    conn.commit()


# ---------------------------------------------------------------------------
# READERS
# ---------------------------------------------------------------------------
def get_queue(conn) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT s.business_id, b.name, b.sector, s.fit_score, b.distance_km, "
        "s.top_driver, s.tier, s.flags, b.warm_intro, "
        "COALESCE(o.status,'new') AS status, o.owner, o.notes, o.updated_at "
        "FROM business_scores s JOIN businesses b USING(business_id) "
        "LEFT JOIN outreach_status o USING(business_id) "
        "ORDER BY s.fit_score DESC", conn)


def get_detail(conn, business_id):
    b = pd.read_sql("SELECT * FROM businesses WHERE business_id=?", conn,
                    params=(business_id,))
    sf = pd.read_sql("SELECT feature,value,confidence,justification,model,derived_at "
                     "FROM soft_features WHERE business_id=?", conn,
                     params=(business_id,))
    sc = pd.read_sql("SELECT * FROM business_scores WHERE business_id=?", conn,
                     params=(business_id,))
    contrib = json.loads(sc.contrib_json.iloc[0]) if len(sc) else {}
    return (b.iloc[0].to_dict() if len(b) else {}), sf, contrib


if __name__ == "__main__":
    c = connect(); init_db(c)
    print("initialised sponsor store at", DB_PATH)
    print("tables:", [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
    print("status vocabulary:", STATUSES)
