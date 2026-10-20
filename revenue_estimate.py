#!/usr/bin/env python3
"""
revenue_estimate.py  —  employee and revenue bands, with visible working
========================================================================
Turns the recorded figures from ingest_accounts.py into what the page shows:

  Employees  a band from the reported average headcount; "Directors only" for 0
  Revenue    1. REPORTED  turnover from the filed accounts, when it's there
             2. ESTIMATE  the ONS average turnover per business for the
                          company's industry and employee size band (the
                          reported headcount picks the size band)
             3. NOT AVAILABLE  no headcount, or an industry ONS doesn't cover

Every estimate carries its working as a sentence and as separate inputs, so a
person can check it against one line of the ONS table. That table lives in
ons_turnover_by_size.csv (built from the ONS file by build_ons_ratios.py), not
in code, so it can be reviewed and updated each year without a code change.

Known limits, stated on the page: every firm in the same industry and size band
gets the same figure (a 2-person and a 9-person joiner look alike), and it's an
average, so a few larger firms pull it up.

Pure functions, no network. python revenue_estimate.py runs the checks.
"""
from __future__ import annotations
import csv
import os

RATIOS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ons_turnover_by_size.csv")
METHOD_VERSION = "rev-est-v2-avg-per-business"

EMPLOYEE_BANDS = [(1, 9, "1–9"), (10, 49, "10–49"), (50, 249, "50–249"), (250, None, "250+")]
REVENUE_BANDS = [(0, 250_000, "Under £250k"), (250_000, 1_000_000, "£250k–£1m"),
                 (1_000_000, 5_000_000, "£1m–£5m"), (5_000_000, None, "£5m+")]

# SIC 2007 division (first two digits) -> section letter
_SECTIONS = [("A", 1, 3), ("B", 5, 9), ("C", 10, 33), ("D", 35, 35), ("E", 36, 39),
             ("F", 41, 43), ("G", 45, 47), ("H", 49, 53), ("I", 55, 56), ("J", 58, 63),
             ("K", 64, 66), ("L", 68, 68), ("M", 69, 75), ("N", 77, 82), ("O", 84, 84),
             ("P", 85, 85), ("Q", 86, 88), ("R", 90, 93), ("S", 94, 96), ("T", 97, 98),
             ("U", 99, 99)]
SECTION_NAMES = {
    "A": "agriculture, forestry and fishing", "B": "mining and quarrying",
    "C": "manufacturing", "D": "electricity and gas supply", "E": "water and waste",
    "F": "construction", "G": "wholesale, retail and motor trade",
    "H": "transport and storage", "I": "accommodation and food",
    "J": "information and communication", "K": "finance and insurance",
    "L": "real estate", "M": "professional, scientific and technical",
    "N": "administrative and support services", "O": "public administration",
    "P": "education", "Q": "health and social work", "R": "arts and recreation",
    "S": "other services", "T": "households as employers", "U": "extraterritorial bodies",
}


def sic_section(code) -> str | None:
    digits = "".join(ch for ch in str(code or "").split(".")[0] if ch.isdigit())
    if not digits:
        return None
    div = int(digits.zfill(5)[:2])
    for letter, lo, hi in _SECTIONS:
        if lo <= div <= hi:
            return letter
    return None


def employee_band(n) -> str | None:
    if n is None:
        return None
    n = int(n)
    if n == 0:
        return "Directors only"
    for lo, hi, label in EMPLOYEE_BANDS:
        if n >= lo and (hi is None or n <= hi):
            return label
    return None


def revenue_band(v) -> str | None:
    if v is None:
        return None
    v = max(0, float(v))
    for lo, hi, label in REVENUE_BANDS:
        if v >= lo and (hi is None or v < hi):
            return label
    return None


def load_ratios(path=RATIOS_PATH) -> list[dict]:
    """Rows of the reviewable ratio table. Missing file -> [] (no estimates)."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["emp_min"] = int(r["emp_min"])
        r["emp_max"] = int(r["emp_max"]) if str(r.get("emp_max") or "").strip() else None
        r["avg_turnover_per_business_gbp"] = float(r["avg_turnover_per_business_gbp"])
        r["sic_prefixes"] = tuple(str(r.get("sic_prefixes") or "").split())
    return rows


def find_ratio(ratios, section, employees, sic=None) -> dict | None:
    """The ONS row for this industry and size band, if the survey covers this
    business's SIC code (partial sections list the prefixes they cover)."""
    digits = "".join(ch for ch in str(sic or "") if ch.isdigit()).zfill(5)
    for r in ratios:
        if r["section"] == section and employees >= r["emp_min"] \
                and (r["emp_max"] is None or employees <= r["emp_max"]):
            if r["sic_prefixes"] and not digits.startswith(r["sic_prefixes"]):
                return None
            return r
    return None


def _when(period_end) -> str:
    if not period_end:
        return "latest accounts"
    from datetime import date
    d = date.fromisoformat(str(period_end)[:10])
    return f"accounts to {d.day} {d.strftime('%b %Y')}"


def assess(acct: dict | None, sic_codes, ratios) -> dict:
    """acct: a row from ingest_accounts.read_accounts (or None).
    Returns everything the page and CSV need, including the working."""
    import ingest_accounts as ia
    acct = acct or {"status": "no_company"}
    status = acct.get("status")
    emp = acct.get("employees") if status == "reported" else None
    turn = acct.get("turnover") if status == "reported" else None
    out = dict(
        employees=emp, employees_band=employee_band(emp),
        employees_basis="reported" if emp is not None else "not_available",
        employees_note=(_when(acct.get("period_end")) if emp is not None
                        else ia.STATUS_TEXT.get(status) or ia.STATUS_TEXT["not_in_accounts"]),
        revenue_basis="not_available", revenue_band=None, revenue_value=None,
        revenue_working=None, est_section=None, est_avg_turnover=None, est_source=None,
        method_version=METHOD_VERSION,
        accounts_period_end=acct.get("period_end"), accounts_source=acct.get("source_url"),
    )

    # 1. reported turnover
    if turn is not None:
        out.update(revenue_basis="reported", revenue_value=turn, revenue_band=revenue_band(turn),
                   revenue_working=f"Turnover of £{turn:,} in the {_when(acct.get('period_end'))}.")
        return out

    # 2. estimate from reported headcount
    if emp is None:
        out["revenue_working"] = "No reported headcount to estimate from."
        return out
    if emp == 0:
        out["revenue_working"] = ("Reports no employees beyond its directors, so there's no "
                                  "headcount to estimate from.")
        return out
    codes = [str(c) for c in (sic_codes or []) if str(c).strip()]
    sic = next((c for c in codes if sic_section(c)), None)
    section = sic_section(sic) if sic else None
    if not section:
        out["revenue_working"] = "No industry code on the company record to estimate from."
        return out
    out["est_section"] = section
    if not ratios:
        out["revenue_working"] = "ONS table not loaded, so no estimate."
        return out
    r = find_ratio(ratios, section, emp, sic)
    if not r:
        out["revenue_working"] = (f"The ONS survey doesn't cover this industry (code {sic}, "
                                  f"{SECTION_NAMES.get(section, section)}), so no estimate.")
        return out
    est = r["avg_turnover_per_business_gbp"]
    size_txt = (f"{r['emp_min']}+" if r["emp_max"] is None else f"{r['emp_min']}–{r['emp_max']}")
    out.update(
        revenue_basis="estimate", revenue_value=round(est), revenue_band=revenue_band(est),
        est_avg_turnover=round(est), est_source=f"{r['source']}, {r['year']} figures",
        revenue_working=(
            f"{emp} employees ({_when(acct.get('period_end'))}) puts it in the {size_txt} size band. "
            f"The average UK {SECTION_NAMES.get(section, section)} business with {size_txt} employees "
            f"turned over £{est:,.0f} in {r['year']} ({r['source']}; industry code {sic}), "
            f"so {revenue_band(est)}. Every firm this size in this industry gets the same figure."))
    return out


# ---------------------------------------------------------------------------
# offline checks
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    assert [employee_band(n) for n in (0, 1, 9, 10, 49, 50, 249, 250, None)] == \
        ["Directors only", "1–9", "1–9", "10–49", "10–49", "50–249", "50–249", "250+", None]
    assert [revenue_band(v) for v in (0, 249_999, 250_000, 999_999, 1_000_000, 4_999_999, 5_000_000)] == \
        ["Under £250k", "Under £250k", "£250k–£1m", "£250k–£1m", "£1m–£5m", "£1m–£5m", "£5m+"]
    assert [sic_section(c) for c in ("49390", "56302", "1110", "69201", "99999", "", None)] == \
        ["H", "I", "A", "M", "U", None, None]
    print("bands and industry sections ✓")

    ratios = [dict(section="H", emp_min=1, emp_max=9, avg_turnover_per_business_gbp=268_915.0,
                   sic_prefixes=(), source="ONS test table", year="2024"),
              dict(section="H", emp_min=10, emp_max=49, avg_turnover_per_business_gbp=4_152_860.0,
                   sic_prefixes=(), source="ONS test table", year="2024"),
              dict(section="Q", emp_min=1, emp_max=9, avg_turnover_per_business_gbp=114_958.0,
                   sic_prefixes=("861", "869", "87", "88"), source="ONS test table", year="2024")]
    rep = dict(status="reported", employees=23, turnover=2_450_000, period_end="2025-03-31")
    a = assess(rep, ["49390"], ratios)
    assert (a["revenue_basis"], a["revenue_band"], a["employees_band"]) == ("reported", "£1m–£5m", "10–49")

    est = dict(status="reported", employees=8, turnover=None, period_end="2025-02-28")
    a = assess(est, ["49390"], ratios)
    assert (a["revenue_basis"], a["revenue_value"], a["revenue_band"]) == ("estimate", 268_915, "£250k–£1m")
    assert "1–9 size band" in a["revenue_working"] and "£268,915" in a["revenue_working"]
    print("estimate working:", a["revenue_working"])
    # partial coverage: physio (86.90) is in the survey, a dental practice (86.23) isn't
    a = assess(est, ["86900"], ratios)
    assert (a["revenue_basis"], a["revenue_band"]) == ("estimate", "Under £250k"), a
    a = assess(est, ["86230"], ratios)
    assert a["revenue_basis"] == "not_available" and "doesn't cover" in a["revenue_working"], a

    a = assess(dict(status="reported", employees=0, period_end="2025-03-31"), ["49390"], ratios)
    assert a["employees_band"] == "Directors only" and a["revenue_basis"] == "not_available"
    a = assess(dict(status="paper_only"), ["49390"], ratios)
    assert a["employees_basis"] == "not_available" and "paper" in a["employees_note"]
    a = assess(est, ["64191"], ratios)            # finance: not in the ratio table
    assert a["revenue_basis"] == "not_available" and "doesn't cover" in a["revenue_working"]
    a = assess(None, [], ratios)
    assert a["employees_note"] == "No Companies House record linked"
    a = assess(est, ["49390"], [])                # table not loaded yet
    assert a["revenue_basis"] == "not_available" and "not loaded" in a["revenue_working"]
    print("reported first, estimate second, honest 'not available' otherwise ✓")
