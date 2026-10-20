#!/usr/bin/env python3
"""
match_sponsors.py  —  which of our businesses appear on club sponsor lists
==========================================================================
Links the club sponsor records (club_sponsors.py) to the businesses in
sponsor.db, and turns Dumbarton's own sponsor pages into recorded connections.

How a sponsor is matched to a business, strongest first:
  website   the sponsor's logo links to the business's own website domain
  name      the names are the same once "Ltd", "The" etc. are dropped, or the
            shorter name (2+ words) sits inside the longer one: the same rule
            connections.csv uses
  weak      names very similar but not the same (e.g. a spelling difference):
            kept, but flagged for a person to check

Everything matched is the club's own record, so it's shown as Reported. It is
display only, with one exception agreed in the spec: DUMBARTON's own sponsors
become recorded connections, like rows in connections.csv:
  on the club's page this season   -> current_sponsor (Renewal)
  only on archived pages           -> past_sponsor (Win-back), latest season noted
A person's entry in connections.csv still wins ("not a fit" always does).

    python match_sponsors.py           offline checks
(the pipeline calls match_all() and club_connection_rows(); see sponsor_pipeline.py)
"""
from __future__ import annotations
import difflib
import sqlite3

import data_checks as dc
from club_sponsors import host, norm

OWN_CLUB = "Dumbarton"
WEAK_MIN = 0.88


def _has_table(conn, name) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def load_club_sponsors(conn) -> list[dict]:
    if not _has_table(conn, "club_sponsors"):
        return []
    cols = ["club", "league", "tier", "near", "sponsor_name", "sponsor_domain", "season",
            "source_type", "source_url"]
    return [dict(zip(cols, r)) for r in conn.execute(f"SELECT {','.join(cols)} FROM club_sponsors")]


def match_one(sponsor_name, sponsor_domain, biz) -> tuple[str, float] | None:
    """biz: dict with name, website. Returns (match_type, score) or None."""
    site = host(biz.get("website") or "")
    if sponsor_domain and site and (sponsor_domain == site or sponsor_domain.endswith("." + site)
                                    or site.endswith("." + sponsor_domain)):
        return ("website", 1.0)
    if dc._same_business(sponsor_name, biz.get("name")):
        return ("name", 0.95)
    a, b = dc._norm_biz(sponsor_name), dc._norm_biz(biz.get("name"))
    if len(a) >= 6 and len(b) >= 6:
        r = difflib.SequenceMatcher(None, a, b).ratio()
        if r >= WEAK_MIN:
            return ("weak", round(r, 2))
    return None


def match_all(conn, rows=None, log=print) -> list[dict]:
    """Rebuild sponsor_matches (disposable: recomputed every run)."""
    rows = rows if rows is not None else load_club_sponsors(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sponsor_matches (
      business_id TEXT, club TEXT, league TEXT, tier INTEGER, near INTEGER, season TEXT,
      sponsor_name TEXT, source_type TEXT, source_url TEXT, match_type TEXT, match_score REAL);
    DELETE FROM sponsor_matches;""")
    biz = [dict(business_id=b, name=n, website=w) for b, n, w in
           conn.execute("SELECT business_id, name, website FROM businesses")]
    out = []
    for r in rows:
        best = None
        for b in biz:
            m = match_one(r["sponsor_name"], r["sponsor_domain"], b)
            if m and (best is None or m[1] > best[1][1]):
                best = (b, m)
        if best:
            b, (mtype, score) = best
            out.append(dict(business_id=b["business_id"], club=r["club"], league=r["league"], tier=r["tier"],
                            near=r["near"], season=r["season"], sponsor_name=r["sponsor_name"],
                            source_type=r["source_type"], source_url=r["source_url"],
                            match_type=mtype, match_score=score))
    conn.executemany("INSERT INTO sponsor_matches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     [tuple(m.values()) for m in out])
    conn.commit()
    if rows:
        nb = len({m["business_id"] for m in out})
        log(f"  football sponsorship: {len(rows)} club sponsor records, {nb} of our businesses matched "
            f"({sum(1 for m in out if m['match_type'] == 'weak')} weak matches flagged to check)")
    return out


def club_connection_rows(rows: list[dict], businesses: list[dict], today_season: str) -> list[dict]:
    """Dumbarton's own sponsor pages as connections.csv-style rows. Matched to
    a business here (by website or name) so a domain-only match still lands."""
    own = [r for r in rows if r["club"] == OWN_CLUB]
    by_biz = {}
    for r in own:
        for b in businesses:
            m = match_one(r["sponsor_name"], r.get("sponsor_domain"), b)
            if m and m[0] != "weak":                       # a weak match never sets a connection
                by_biz.setdefault(b["name"], []).append(r)
    out = []
    for name, rs in by_biz.items():
        seasons = sorted({r["season"] for r in rs})
        if today_season in seasons:
            out.append(dict(match=name, kind="current_sponsor",
                            note=f"on Dumbarton FC's sponsor page, {today_season}"))
        else:
            out.append(dict(match=name, kind="past_sponsor",
                            note=f"on Dumbarton FC's sponsor page in {', '.join(seasons)} (archived page)"))
    return out


if __name__ == "__main__":
    print("sponsor matching — offline checks\n" + "=" * 40)
    biz = [dict(business_id="b1", name="Marbill Coaches", website="https://marbillcoaches.example/"),
           dict(business_id="b2", name="Leven Valley Joinery Ltd", website=None),
           dict(business_id="b3", name="The Anchor Inn", website=None),
           dict(business_id="b4", name="Algas", website="https://algas.example")]
    assert match_one("M Coaches", "marbillcoaches.example", biz[0]) == ("website", 1.0)
    assert match_one("Leven Valley Joinery", None, biz[1])[0] == "name"
    assert match_one("Anchor Inn", None, biz[2])[0] == "name"          # "The" dropped: same name
    assert match_one("The Anchor Inn Dumbarton", None, biz[2])[0] == "name"
    assert match_one("Levan Valley Joinery", None, biz[1])[0] == "weak"
    assert match_one("Galgas", None, biz[3]) is None                    # short names never fuzzy-match
    print("  website, name, weak and no-match cases ✓")

    rows = [dict(club="Dumbarton", league="L2", tier=4, near=1, sponsor_name="Marbill Coaches",
                 sponsor_domain="marbillcoaches.example", season="2026/27", source_type="club_page", source_url="u"),
            dict(club="Dumbarton", league="L2", tier=4, near=1, sponsor_name="Leven Valley Joinery",
                 sponsor_domain=None, season="2023/24", source_type="archived_page", source_url="u"),
            dict(club="Vale of Leven", league="WoSFL", tier=9, near=1, sponsor_name="Leven Valley Joinery",
                 sponsor_domain=None, season="2025/26", source_type="archived_page", source_url="u")]
    conn_rows = club_connection_rows(rows, biz, "2026/27")
    assert {(r["match"], r["kind"]) for r in conn_rows} == {("Marbill Coaches", "current_sponsor"),
                                                            ("Leven Valley Joinery Ltd", "past_sponsor")}, conn_rows
    print("  Dumbarton's own page -> Renewal (this season) / Win-back (archive only) ✓")

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE businesses (business_id TEXT, name TEXT, website TEXT)")
    conn.executemany("INSERT INTO businesses VALUES (?,?,?)",
                     [(b["business_id"], b["name"], b["website"]) for b in biz])
    out = match_all(conn, rows, log=lambda *_: None)
    assert sorted((m["business_id"], m["club"], m["season"]) for m in out) == [
        ("b1", "Dumbarton", "2026/27"), ("b2", "Dumbarton", "2023/24"), ("b2", "Vale of Leven", "2025/26")]
    print("  matches written: Marbill (Dumbarton), Leven Valley (Dumbarton, Vale of Leven) ✓")
