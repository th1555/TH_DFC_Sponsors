#!/usr/bin/env python3
"""
sic_sectors.py  —  map UK SIC 2007 codes to the sponsor tool's sector taxonomy
==============================================================================
Companies House hands back raw 5-digit SIC codes ("56302", "49410"). The scoring
engine speaks in 13 human sectors ("pubs_bars_hospitality", ...). This module is
the translator — the sponsor-side equivalent of league_info.py making FotMob's
league ids legible.

How it resolves a code:
  1. SPECIFIC  — exact 5-digit overrides where a division splits across sectors
     (e.g. 56302 "public houses and bars" -> pubs, 56103 "takeaway" -> food).
  2. DIVISION  — the 2-digit SIC division as a sensible fallback
     (e.g. 41/42/43 Construction -> trades; 47 Retail -> independent retail).
  3. unknown   — anything unmapped, so it's reviewed, never silently mis-sectored.

A company usually lists SEVERAL SIC codes. map_sectors() resolves each and picks
by a priority order that favours the specific, local-affinity sectors over broad
buckets — so a firm coded both "retail" and "funeral director" reads as a funeral
director, which is what matters for sponsorship.

Note: `national_chain` is NOT a SIC outcome — it's a scale/decision-locus judgment
made elsewhere (many outlets, non-local HQ). SIC tells you WHAT a business does,
not whether its sponsorship decision is made in the town.

Pure logic, no network — fully testable offline.  python sic_sectors.py  runs checks.
"""
from __future__ import annotations

# 1) exact 5-digit overrides (only where a division must split across sectors)
SPECIFIC = {
    # food & beverage service (division 56) splits: pubs vs food
    "56301": "pubs_bars_hospitality",      # licensed clubs
    "56302": "pubs_bars_hospitality",      # public houses and bars
    "56101": "food_takeaway_restaurants",  # licensed restaurants
    "56102": "food_takeaway_restaurants",  # unlicensed restaurants & cafes
    "56103": "food_takeaway_restaurants",  # takeaway food shops & mobile stands
    "56210": "food_takeaway_restaurants",  # event catering
    "56290": "food_takeaway_restaurants",  # other food services
    # retail (division 47) splits: online vs fuel vs the rest
    "47910": "online_ecommerce",           # retail via mail order / internet
    "47300": "motor_dealers_garages",      # retail of automotive fuel
    # construction (division 43) splits: heating/renewables out of general trades
    "43220": "utilities_renewables_heating",  # plumbing, heat & air-con install
    "43210": "utilities_renewables_heating",  # electrical installation
    # personal services (division 96) — funeral is its own loyal sector
    "96030": "funeral_directors",          # funeral and related activities
    # dormant / nonclassifiable
    "99999": "unknown",                    # dormant company
    "98000": "unknown",                    # residents property management
    "74990": "local_professional_services",  # other professional/technical n.e.c.
}

# 2) 2-digit SIC division -> sector (the fallback)
DIVISION = {
    "35": "utilities_renewables_heating",  # electricity, gas, steam supply
    "41": "building_construction_trades",  # construction of buildings
    "42": "building_construction_trades",  # civil engineering
    "43": "building_construction_trades",  # specialised construction (trades)
    "45": "motor_dealers_garages",         # sale/repair of motor vehicles
    "47": "independent_retail",            # retail trade (overrides above handle splits)
    "49": "transport_coaches_haulage",     # land transport (coaches, taxis, haulage)
    "50": "transport_coaches_haulage",     # water transport
    "52": "transport_coaches_haulage",     # warehousing & transport support
    "55": "pubs_bars_hospitality",         # accommodation
    "56": "food_takeaway_restaurants",     # food & beverage (overrides split it)
    "64": "local_professional_services",   # financial services
    "65": "local_professional_services",   # insurance
    "66": "local_professional_services",   # auxiliary financial
    "68": "local_professional_services",   # real estate (incl. estate agents)
    "69": "local_professional_services",   # legal & accounting
    "70": "local_professional_services",   # head office / management consultancy
    "71": "local_professional_services",   # architecture & engineering
    "73": "local_professional_services",   # advertising & market research
    "74": "local_professional_services",   # other professional, scientific, technical
    "77": "local_professional_services",   # rental & leasing
    "84": "public_sector_charity",         # public administration
    "85": "public_sector_charity",         # education
    "86": "health_fitness_physio_dental",  # human health (GPs, dental, physio)
    "87": "public_sector_charity",         # residential care
    "88": "public_sector_charity",         # social work
    "93": "health_fitness_physio_dental",  # sports & fitness facilities
    "94": "public_sector_charity",         # membership organisations
    "96": "independent_retail",            # other personal services (96030 funeral split out)
}

# priority when a company lists several SIC codes: specific local sectors first,
# broad buckets and non-local last. First match in this order wins.
PRIORITY = [
    "funeral_directors",
    "transport_coaches_haulage",
    "building_construction_trades",
    "utilities_renewables_heating",
    "motor_dealers_garages",
    "pubs_bars_hospitality",
    "food_takeaway_restaurants",
    "health_fitness_physio_dental",
    "local_professional_services",
    "independent_retail",
    "online_ecommerce",
    "public_sector_charity",
    "unknown",
]


def sic_to_sector(code) -> str:
    """One 5-digit SIC code -> one sector. Specific override, then division, then unknown."""
    c = str(code or "").strip().split(".")[0]      # tolerate "56302" / "56302.0"
    c = "".join(ch for ch in c if ch.isdigit())
    if not c:
        return "unknown"
    c = c.zfill(5)[:5]
    if c in SPECIFIC:
        return SPECIFIC[c]
    return DIVISION.get(c[:2], "unknown")


def map_sectors(sic_codes) -> str:
    """A company's list of SIC codes -> its single best sector, by PRIORITY.
    Empty / all-unknown -> 'unknown' so it's reviewed, never silently bucketed."""
    if not sic_codes:
        return "unknown"
    resolved = {sic_to_sector(c) for c in sic_codes}
    for sector in PRIORITY:
        if sector in resolved:
            # don't let 'unknown' win if any real sector was found
            if sector == "unknown" and len(resolved) > 1:
                continue
            return sector
    return "unknown"


# ---------------------------------------------------------------------------
# offline self-checks
# ---------------------------------------------------------------------------
_CASES = [
    (["56302"], "pubs_bars_hospitality"),          # a pub
    (["56103"], "food_takeaway_restaurants"),      # a takeaway
    (["49410"], "transport_coaches_haulage"),      # road haulage (division 49)
    (["49390"], "transport_coaches_haulage"),      # other passenger land (coaches)
    (["43999", "41201"], "building_construction_trades"),
    (["43220"], "utilities_renewables_heating"),   # plumbing/heating override
    (["45200"], "motor_dealers_garages"),          # vehicle maintenance
    (["47300"], "motor_dealers_garages"),          # fuel retail override
    (["47910"], "online_ecommerce"),               # internet retail override
    (["47110"], "independent_retail"),             # grocery retail (division fallback)
    (["69201"], "local_professional_services"),    # accountants
    (["68310"], "local_professional_services"),    # estate agents
    (["86230"], "health_fitness_physio_dental"),   # dental
    (["93130"], "health_fitness_physio_dental"),   # fitness facilities
    (["96030"], "funeral_directors"),              # funeral override
    (["99999"], "unknown"),                        # dormant
    ([], "unknown"),                               # none listed
    # multi-code: priority picks the specific local sector over broad retail
    (["47990", "96030"], "funeral_directors"),
    (["82990", "47110"], "independent_retail"),    # unknown + retail -> retail, not unknown
]

if __name__ == "__main__":
    print("SIC -> sector mapping — offline checks\n" + "=" * 46)
    ok = 0
    for codes, expect in _CASES:
        got = map_sectors(codes)
        flag = "✓" if got == expect else "✗ FAIL"
        if got == expect:
            ok += 1
        print(f"  {flag}  {str(codes):<22} -> {got}"
              + ("" if got == expect else f"   (expected {expect})"))
    print("=" * 46)
    print(f"{ok}/{len(_CASES)} passed")
    print(f"\n{len(SPECIFIC)} specific overrides, {len(DIVISION)} division mappings, "
          f"{len(PRIORITY)} sectors in priority order")
