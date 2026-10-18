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


# Companies House allows roughly 600 requests per 5 minutes per key. A real run
# makes ~3 calls per business (search, profile, officers), so pace calls and wait
# out a 429 instead of crashing half-way through a long Colab run.
MIN_INTERVAL_S = 0.55
_last_call = [0.0]


def _get(path, api_key=None, params=None, tries=5):
    import time
    import requests
    for attempt in range(tries):
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        r = requests.get(f"{BASE}{path}", auth=_auth(api_key), params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            pause = int(r.headers.get("Retry-After") or 0) or min(60, 5 * 2 ** attempt)
            print(f"  [companies house] {r.status_code}, waiting {pause}s and retrying")
            time.sleep(pause)
            continue
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def search_companies_api(query, api_key=None, n=20):
    return _get("/search/companies", api_key, {"q": query, "items_per_page": n}).get("items", [])


def get_profile_api(company_number, api_key=None):
    return _get(f"/company/{company_number}", api_key)


def get_officers_api(company_number, api_key=None):
    return _get(f"/company/{company_number}/officers", api_key, {"items_per_page": 50}).get("items", [])


def local_director_count(officers, districts) -> tuple[int, int]:
    """(directors based in a local postcode district, active directors).
    Uses the officer's service address; keeps only the count, never the
    name or address."""
    import data_checks as dc
    local = {d.upper() for d in districts}
    active = [o for o in officers or []
              if not o.get("resigned_on")
              and "director" in str(o.get("officer_role") or "").lower()]
    n_local = sum(1 for o in active
                  if dc.outward((o.get("address") or {}).get("postal_code")) in local)
    return n_local, len(active)


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
    # the Oct 2026 failure, reproduced: a same-named company registered in Wales
    "castle garage": dict(company_number="00205581", company_name="CASTLE GARAGE LIMITED",
        company_status="active", date_of_creation="1925-01-01", sic_codes=["45200"],
        accounts={"last_accounts": {"type": "small"}},
        registered_office_address={"postal_code": "SA20 0AA", "locality": "Llandovery"}),
    "dormant holdings": dict(company_number="SC000007", company_name="DORMANT HOLDINGS LTD",
        company_status="active", date_of_creation="2012-03-30", sic_codes=["99999"],
        accounts={"last_accounts": {"type": "dormant"}},
        registered_office_address={"postal_code": "G82 4AA", "locality": "Dumbarton"}),
}


_FIX_OFFICERS = {
    "SC000001": [{"officer_role": "director", "address": {"postal_code": "G82 2AB"}},
                 {"officer_role": "director", "address": {"postal_code": "G2 7DA"}}],
}


def get_officers_fixture(company_number, **_):
    return _FIX_OFFICERS.get(company_number, [])


def search_companies_fixture(query, **_):
    q = _norm(query)
    items = []
    for key, prof in _FIX_PROFILES.items():
        if q and (q in key or key in q or name_score(query, prof["company_name"]) > 0.4):
            items.append({"company_number": prof["company_number"],
                          "title": prof["company_name"],
                          "company_status": prof["company_status"],
                          "address": prof.get("registered_office_address") or {}})
    return items


def get_profile_fixture(company_number, **_):
    for prof in _FIX_PROFILES.values():
        if prof["company_number"] == company_number:
            return prof
    raise KeyError(company_number)


# ---------------------------------------------------------------------------
# ENRICH — name -> best CH match -> parsed fields (+ a match flag)
# ---------------------------------------------------------------------------
def enrich(name, cfg, mode="fixture", api_key=None, today=None, place_postcode=None) -> dict:
    """Best Companies House match for a business found on Google Places.

    A candidate is accepted only if ALL of these hold:
      * the company is active (a dissolved or liquidated company can't be the
        business trading at that address today);
      * its name clears `match_min_score`;
      * it agrees on place with the business (data_checks.location_ok): same
        postcode district, or registered in Scotland / office in Scotland.
    Among the candidates that pass, the best name match wins. So a similarly
    named company in England can no longer beat the right one in Dumbarton, and
    if only the wrong-place one exists, nothing is linked and the row is flagged.
    """
    import data_checks as dc
    search = search_companies_fixture if mode == "fixture" else search_companies_api
    get_profile = get_profile_fixture if mode == "fixture" else get_profile_api
    floor = cfg["ingest"]["companies_house"]["match_min_score"]

    items = search(name, api_key=api_key)
    scored = sorted(((name_score(name, it.get("title", "")), it) for it in items),
                    key=lambda t: -t[0])
    best_name_score = scored[0][0] if scored else 0.0

    rejected_place = None
    for sc, it in scored:
        if sc < floor:
            break
        if str(it.get("company_status") or "active").lower() != "active":
            continue
        item_pc = (it.get("address") or {}).get("postal_code")
        if item_pc and not dc.location_ok(place_postcode, item_pc, it.get("company_number")):
            rejected_place = rejected_place or it
            continue
        parsed = parse_profile(get_profile(it["company_number"], api_key=api_key), today)
        # the search snippet can be stale: re-check against the full profile
        if not dc.location_ok(place_postcode, parsed["postcode"], parsed["company_number"]):
            rejected_place = rejected_place or it
            continue
        parsed["ch_match_score"] = round(sc, 2)
        parsed["company_known"] = True
        try:
            get_officers = get_officers_fixture if mode == "fixture" else get_officers_api
            districts = (cfg.get("connections") or {}).get("local_districts", [])
            parsed["directors_local"], parsed["directors_total"] = local_director_count(
                get_officers(parsed["company_number"], api_key=api_key), districts)
        except Exception as e:                      # officers are a bonus, never fatal
            print(f"  [companies house] officers for {parsed['company_number']} failed: {e}")
        parsed["ch_flag"] = None if sc >= 0.8 else "weak Companies House match — verify"
        return parsed

    if rejected_place:
        flag = (f"name matched company {rejected_place.get('company_number')}, registered "
                f"outside Scotland — not linked, verify")
    else:
        flag = "unmatched — no confident Companies House match"
    return {"sector": "unknown", "company_age": 0, "size_band": "unknown",
            "accounts_dormant": False, "postcode": None, "company_number": None,
            "company_known": False, "ch_match_score": round(best_name_score, 2),
            "ch_flag": flag}


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

    # location check: the real Oct 2026 failure must no longer link
    r = enrich("Castle Garage", cfg, mode="fixture", today=TODAY, place_postcode="G82 1RQ")
    assert r["company_number"] is None and "outside Scotland" in r["ch_flag"], r
    r = enrich("Marbill Coaches", cfg, mode="fixture", today=TODAY, place_postcode="G82 1AB")
    assert r["company_number"] == "SC000001", r
    assert (r["directors_local"], r["directors_total"]) == (1, 2), r
    print("location check: Welsh 'Castle Garage' rejected for a Dumbarton garage; "
          "local Marbill still linked ✓")
