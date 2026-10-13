#!/usr/bin/env python3
"""
ingest_companies_house.py  —  enrich a business via the Companies House API
===========================================================================
Takes a business NAME (from Places) and returns the structured facts the scorer
needs: sector (via sic_sectors), company age, size band, dormant flag, postcode.

Companies House is a FREE official REST API (register for a key at
developer.company-information.service.gov.uk). Auth is HTTP Basic with the key as
the username and an empty password. Because it's an official API it doesn't block
datacenter IPs — so, unlike the recruitment scrapers, this part can run anywhere.

The match is fuzzy: a name search can return several companies, so we score each
candidate's name against the query and accept the best only if it clears
`companies_house.match_min_score` in the config — otherwise the business is kept
but FLAGGED "unmatched", exactly like the recruitment tool leaves an ambiguous
FotMob/Transfermarkt pair unmatched rather than guessing.

Two modes, identical output:
  mode="fixture" (default) — canned CH profiles in the real response shape; offline.
  mode="api"              — the real calls. Needs COMPANIES_HOUSE_API_KEY in env.

python ingest_companies_house.py   runs the offline parse checks.
"""
from __future__ import annotations
import os
import re
import difflib
from datetime import date, datetime

BASE = "https://api.company-information.service.gov.uk"

# accounts category (CH "last_accounts.type") -> our size band
_ACCOUNTS_SIZE = {
    "micro-entity": "micro",
    "small": "small", "total-exemption-small": "small", "total-exemption-full": "small",
    "unaudited-abridged": "small", "abbreviated": "small",
    "audit-exemption-subsidiary": "small", "filing-exemption-subsidiary": "small",
    "medium": "medium",
    "full": "large", "group": "large",
    "dormant": "micro",
}


def _norm(s):
    s = re.sub(r"\b(ltd|limited|plc|llp|co|company|the)\b", "", str(s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def name_score(a, b) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def company_age(date_of_creation, today=None) -> int:
    if not date_of_creation:
        return 0
    try:
        d = datetime.strptime(str(date_of_creation)[:10], "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = today or date.today()
    return max(0, int((today - d).days / 365.25))


# ---------------------------------------------------------------------------
# PARSE — a CH company profile (dict) -> our fields.  Pure, fully testable.
# ---------------------------------------------------------------------------
def parse_profile(profile: dict, today=None) -> dict:
    import sic_sectors
    sic = [str(c) for c in (profile.get("sic_codes") or [])]
    sector = sic_sectors.map_sectors(sic)
    acct_type = ((profile.get("accounts") or {}).get("last_accounts") or {}).get("type")
    size = _ACCOUNTS_SIZE.get(str(acct_type or "").lower(), "unknown")
    status = str(profile.get("company_status") or "").lower()
    dormant = (str(acct_type).lower() == "dormant") or ("99999" in sic) \
        or (status and status != "active")
    roa = profile.get("registered_office_address") or {}
    return {
        "company_number": profile.get("company_number"),
        "ch_name": profile.get("company_name"),
        "sector": sector,
        "company_age": company_age(profile.get("date_of_creation"), today),
        "size_band": size,
        "accounts_dormant": bool(dormant),
        "postcode": roa.get("postal_code"),
        "ch_status": status or None,
        "sic_codes": sic,
    }


# ---------------------------------------------------------------------------
# REAL API
# ---------------------------------------------------------------------------
def _auth(api_key=None):
    api_key = api_key or os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        raise RuntimeError("no COMPANIES_HOUSE_API_KEY — use mode='fixture' to run offline")
    return (api_key, "")


def search_companies_api(query, api_key=None, n=20):
    import requests
    r = requests.get(f"{BASE}/search/companies", auth=_auth(api_key),
                     params={"q": query, "items_per_page": n}, timeout=30)
    r.raise_for_status()
    return r.json().get("items", [])


def get_profile_api(company_number, api_key=None):
    import requests
    r = requests.get(f"{BASE}/company/{company_number}", auth=_auth(api_key), timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# OFFLINE FIXTURE  (real-shaped CH profiles, keyed by normalised name)
# ---------------------------------------------------------------------------
_FIX_PROFILES = {
    "marbill coaches": dict(company_number="SC000001", company_name="MARBILL COACHES LTD",
        company_status="active", date_of_creation="1987-04-12", sic_codes=["49390", "49410"],
        accounts={"last_accounts": {"type": "small"}},
        registered_office_address={"postal_code": "G82 1AB", "locality": "Dumbarton"}),
    "the anchor inn": dict(company_number="SC000002", company_name="THE ANCHOR INN (DUMBARTON) LTD",
        company_status="active", date_of_creation="2003-09-01", sic_codes=["56302"],
        accounts={"last_accounts": {"type": "total-exemption-small"}},
        registered_office_address={"postal_code": "G82 1QS", "locality": "Dumbarton"}),
    "bonhill funeralcare": dict(company_number="SC000003", company_name="BONHILL FUNERALCARE LIMITED",
        company_status="active", date_of_creation="1985-02-20", sic_codes=["96030"],
        accounts={"last_accounts": {"type": "micro-entity"}},
        registered_office_address={"postal_code": "G83 9JX", "locality": "Alexandria"}),
    "leven valley joinery": dict(company_number="SC000004", company_name="LEVEN VALLEY JOINERY LTD",
        company_status="active", date_of_creation="2010-06-15", sic_codes=["43320", "41201"],
        accounts={"last_accounts": {"type": "micro-entity"}},
        registered_office_address={"postal_code": "G82 3PD", "locality": "Dumbarton"}),
    "dumbarton motors": dict(company_number="SC000005", company_name="DUMBARTON MOTORS LTD",
        company_status="active", date_of_creation="1998-11-03", sic_codes=["45200", "47300"],
        accounts={"last_accounts": {"type": "small"}},
        registered_office_address={"postal_code": "G82 2RX", "locality": "Dumbarton"}),
    "bytebargains": dict(company_number="SC000006", company_name="BYTEBARGAINS ONLINE LTD",
        company_status="active", date_of_creation="2021-01-18", sic_codes=["47910"],
        accounts={"last_accounts": {"type": "medium"}},
        registered_office_address={"postal_code": "G81 1AA", "locality": "Clydebank"}),
    "dormant holdings": dict(company_number="SC000007", company_name="DORMANT HOLDINGS LTD",
        company_status="active", date_of_creation="2012-03-30", sic_codes=["99999"],
        accounts={"last_accounts": {"type": "dormant"}},
        registered_office_address={"postal_code": "G82 4AA", "locality": "Dumbarton"}),
}


def search_companies_fixture(query, **_):
    q = _norm(query)
    items = []
    for key, prof in _FIX_PROFILES.items():
        if q and (q in key or key in q or name_score(query, prof["company_name"]) > 0.4):
            items.append({"company_number": prof["company_number"],
                          "title": prof["company_name"],
                          "company_status": prof["company_status"]})
    return items


def get_profile_fixture(company_number, **_):
    for prof in _FIX_PROFILES.values():
        if prof["company_number"] == company_number:
            return prof
    raise KeyError(company_number)


# ---------------------------------------------------------------------------
# ENRICH — name -> best CH match -> parsed fields (+ a match flag)
# ---------------------------------------------------------------------------
def enrich(name, cfg, mode="fixture", api_key=None, today=None) -> dict:
    search = search_companies_fixture if mode == "fixture" else search_companies_api
    get_profile = get_profile_fixture if mode == "fixture" else get_profile_api
    floor = cfg["ingest"]["companies_house"]["match_min_score"]

    items = search(name, api_key=api_key)
    best, best_score = None, 0.0
    for it in items:
        sc = name_score(name, it.get("title", ""))
        if sc > best_score:
            best, best_score = it, sc

    if not best or best_score < floor:
        return {"sector": "unknown", "company_age": 0, "size_band": "unknown",
                "accounts_dormant": False, "postcode": None, "company_number": None,
                "ch_match_score": round(best_score, 2),
                "ch_flag": "unmatched — no confident Companies House match"}

    parsed = parse_profile(get_profile(best["company_number"], api_key=api_key), today)
    parsed["ch_match_score"] = round(best_score, 2)
    parsed["ch_flag"] = None if best_score >= 0.8 else "weak Companies House match — verify"
    return parsed


# ---------------------------------------------------------------------------
# offline checks
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open("sponsor_config.yaml"))
    TODAY = date(2026, 1, 1)
    print("Companies House enrichment — offline fixture checks\n" + "=" * 58)
    for q in ["Marbill Coaches", "The Anchor Inn", "Bonhill Funeralcare",
              "Leven Valley Joinery", "Dumbarton Motors", "ByteBargains Online",
              "Dormant Holdings", "Some Business We Can't Find"]:
        r = enrich(q, cfg, mode="fixture", today=TODAY)
        print(f"\n■ query: {q!r}")
        print(f"   sector={r['sector']}  age={r['company_age']}  size={r['size_band']}"
              f"  dormant={r['accounts_dormant']}  postcode={r['postcode']}")
        print(f"   match={r['ch_match_score']}  flag={r['ch_flag']}")
    # direct parse check
    prof = _FIX_PROFILES["marbill coaches"]
    p = parse_profile(prof, today=TODAY)
    assert p["sector"] == "transport_coaches_haulage", p["sector"]
    assert p["company_age"] == 38, p["company_age"]       # 1987 -> 2026
    assert p["size_band"] == "small"
    print("\nparse_profile sanity: Marbill -> transport/coaches, age 38, small ✓")
