#!/usr/bin/env python3
"""
data_checks.py  —  data-quality rules shared by the pipeline and the repair script
==================================================================================
The first real run (Oct 2026) showed three ways bad data was getting into the
"facts only" Local fit score. Each has a rule here, used in two places:
sponsor_pipeline.py (live runs) and repair_published_data.py (fixing an
already-built dataset without re-calling the APIs).

1. LOCATION CHECK on Companies House matches. The match used to be on name
   alone, so "Castle Garage, Glasgow Rd, Dumbarton" could be linked to a company
   of the same name registered in Carmarthenshire. A match now has to agree on
   place: same postcode district as the business, or a company registered in
   Scotland / with its registered office in Scotland. A name-only match to a
   company registered elsewhere is NOT linked and is flagged for checking.

2. CHAIN DETECTION. SIC codes say what a business does, never whether the
   sponsorship decision is made in the town, so `national_chain` never came out
   of the data and B&M / Toolstation ranked in the top 10. Chains are now caught
   by name and, more reliably, by their website domain (branch pages of a
   national site). Lists live in sponsor_config.yaml under `chains:`.

3. SECTOR SANITY. When the sector implied by the business's own name (or its
   Google Places type) disagrees with the matched company's SIC code, the
   name/type wins and the row is flagged, because the Places record is what the
   business actually is; the Companies House match is the part that can be wrong.

All pure functions, no network.  python data_checks.py  runs the checks.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse

# --- postcodes ---------------------------------------------------------------
_PC = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)

# Postcode areas wholly or mainly in Scotland
SCOTTISH_AREAS = {"AB", "DD", "DG", "EH", "FK", "G", "HS", "IV", "KA", "KW",
                  "KY", "ML", "PA", "PH", "TD", "ZE"}
# Companies House number prefixes for Scottish registrations
# (SC company, SO LLP, SL limited partnership, SA/SF/SZ other Scottish forms)
SCOTTISH_PREFIXES = ("SC", "SO", "SL", "SA", "SF", "SZ")


def postcode_of(text) -> str | None:
    """First full UK postcode in a string, normalised to 'G82 1QL'."""
    m = _PC.search(str(text or ""))
    return f"{m.group(1).upper()} {m.group(2).upper()}" if m else None


def outward(pc) -> str | None:
    p = postcode_of(pc) or str(pc or "").strip().upper()
    return p.split()[0] if p else None


def area(pc) -> str | None:
    out = outward(pc)
    m = re.match(r"[A-Z]+", out or "")
    return m.group(0) if m else None


def location_agreement(place_postcode, ch_postcode, company_number) -> str:
    """How well a Companies House record agrees with where the business is.
      'district'  same postcode district (G82 = G82)         -> strong
      'scotland'  registered in Scotland, or registered office in Scotland
                  (small Scottish firms often use a Glasgow accountant's
                  address, so this is accepted, but weaker)
      'elsewhere' an English/Welsh/NI registration with an office outside
                  Scotland -> for a local Scottish business, almost certainly
                  the wrong company with a similar name
    """
    if place_postcode and ch_postcode and outward(place_postcode) == outward(ch_postcode):
        return "district"
    num = str(company_number or "").upper()
    if num.startswith(SCOTTISH_PREFIXES) or area(ch_postcode) in SCOTTISH_AREAS:
        return "scotland"
    return "elsewhere"


def location_ok(place_postcode, ch_postcode, company_number) -> bool:
    # If we don't know where the business is, we can't use place to judge.
    if not place_postcode or area(place_postcode) not in SCOTTISH_AREAS:
        return True
    return location_agreement(place_postcode, ch_postcode, company_number) != "elsewhere"


# --- chains ------------------------------------------------------------------
def _domain(url) -> str:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def chain_reason(name, website, cfg) -> str | None:
    """Return why this looks like a national chain / franchise branch, or None."""
    ch = (cfg or {}).get("chains") or {}
    dom = _domain(website)
    for d in ch.get("domains", []):
        d = d.lower()
        if dom == d or dom.endswith("." + d):
            return f"website is a branch page on {d}"
    n = f" {re.sub(r'[^a-z0-9&]+', ' ', str(name or '').lower())} "
    for brand in ch.get("names", []):
        b = re.sub(r"[^a-z0-9&]+", " ", brand.lower()).strip()
        if f" {b} " in n:
            return f"name matches chain '{brand}'"
    return None


# --- sector from the business's own name / Google type -------------------------
# Order matters: the first rule that matches wins.
_NAME_RULES = [
    ("funeral_directors",            r"funeral|funeralcare"),
    ("public_sector_charity",        r"health centre|council|trusted trader"),
    ("motor_dealers_garages",        r"garage|motors?|autos?|auto centre|car sales|cars|car clinic|"
                                     r"vehicle|mot|tyres?|accident repair"),
    ("transport_coaches_haulage",    r"coach(es)?|mini ?bus(es)?|buses|haulage|transport|logistics|travel"),
    ("industrial_b2b_services",      r"print|printing|printers|signs|signage|engineering|engineers|fabrication|"
                                     r"industrial|tools|machinery|welding"),
    ("utilities_renewables_heating", r"plumbing|plumber|heating|gas|electrics?|electrical|renewables?|solar|boilers?"),
    ("building_construction_trades", r"joiners?|joinery|builders?|building|construction|roofing|contracts|"
                                     r"home improvements|carpentry|tiling|tilers?|scaffolding"),
    ("local_professional_services",  r"solicitors?|lawyers?|notaries|accountants?|accountancy|accounting|tax|"
                                     r"estate agents?|lettings?|letting agents?|estates|chartered"),
    ("health_fitness_physio_dental", r"dental|dentist|physio|physiotherapy|osteopaths?|clinic|fitness|gym"),
    ("food_takeaway_restaurants",    r"restaurant|takeaway|kebab|chippy|fish (and|&) chips|pizza|grill|tandoori|"
                                     r"curry|spice|spiced|spicy|chinese|cafe|bistro|diner|fry|bbq|barbeque|barbecue"),
    ("pubs_bars_hospitality",        r"bar|inn|pub|lounge|hotel|tavern|arms|country club"),
    ("independent_retail",           r"hardware|supplies|shop|sheds|paint"),
]
_NAME_RULES = [(s, re.compile(rf"\b({p})\b", re.I)) for s, p in _NAME_RULES]

# Google Places (New) primary types -> sector
_TYPE_RULES = {
    "funeral_home": "funeral_directors",
    "car_repair": "motor_dealers_garages", "car_dealer": "motor_dealers_garages",
    "car_wash": "motor_dealers_garages", "gas_station": "motor_dealers_garages",
    "plumber": "utilities_renewables_heating", "electrician": "utilities_renewables_heating",
    "general_contractor": "building_construction_trades",
    "roofing_contractor": "building_construction_trades",
    "lawyer": "local_professional_services", "accounting": "local_professional_services",
    "real_estate_agency": "local_professional_services", "insurance_agency": "local_professional_services",
    "dentist": "health_fitness_physio_dental", "physiotherapist": "health_fitness_physio_dental",
    "gym": "health_fitness_physio_dental", "doctor": "health_fitness_physio_dental",
    "restaurant": "food_takeaway_restaurants", "meal_takeaway": "food_takeaway_restaurants",
    "meal_delivery": "food_takeaway_restaurants", "cafe": "food_takeaway_restaurants",
    "bar": "pubs_bars_hospitality", "pub": "pubs_bars_hospitality",
    "lodging": "pubs_bars_hospitality", "hotel": "pubs_bars_hospitality",
    "hardware_store": "independent_retail", "home_goods_store": "independent_retail",
}


def sector_from_name(name) -> str | None:
    for sector, rx in _NAME_RULES:
        if rx.search(str(name or "")):
            return sector
    return None


# Website host keywords (no word boundaries: "adairssolicitors.com"). Only
# strong, unambiguous stems, so "bar" inside "barbeque" etc. can't misfire.
_HOST_RULES = [
    ("funeral_directors",            ("funeral",)),
    ("motor_dealers_garages",        ("garage", "motors", "autos", "vauxhall", "carsales")),
    ("transport_coaches_haulage",    ("coaches", "haulage")),
    ("utilities_renewables_heating", ("plumb", "heating")),
    ("building_construction_trades", ("joiner", "builder", "roofing")),
    ("local_professional_services",  ("solicitor", "lawyer", "accountan", "estateagent")),
    ("health_fitness_physio_dental", ("dental", "physio")),
    ("food_takeaway_restaurants",    ("takeaway", "restaurant", "pizza", "kebab", "tandoori", "curry")),
]


def sector_from_website(website) -> str | None:
    host = _domain(website).replace("-", "")
    for sector, stems in _HOST_RULES:
        if any(st in host for st in stems):
            return sector
    return None


def sector_from_types(types) -> str | None:
    for t in types or []:
        if t in _TYPE_RULES:
            return _TYPE_RULES[t]
    return None


# --- the one entry point the pipeline + repair script call ------------------
UNLINKED = dict(company_number=None, company_age=0, size_band="unknown",
                accounts_dormant=False, postcode=None, company_known=False)


def reconcile(rec: dict, cfg: dict, place_types=None) -> dict:
    """Apply the three rules to one enriched record (in place) and return it.
    Expects: name, website, address, sector, company_number, postcode (the
    registered-office postcode from Companies House), ch_flag."""
    notes = []
    place_pc = postcode_of(rec.get("address"))

    # 1) location check on the Companies House link
    if rec.get("company_number") and not location_ok(place_pc, rec.get("postcode"),
                                                      rec["company_number"]):
        old = rec["company_number"]
        rec.update(UNLINKED, sector="unknown")
        rec["ch_flag"] = (f"name matched company {old}, registered outside Scotland — "
                          f"not linked, verify")
    rec.setdefault("company_known", bool(rec.get("company_number")))
    # Accepted only on "registered in Scotland", with an office outside the home
    # region (e.g. an Edinburgh LLP for a Dumbarton car dealer): keep, but check.
    home = set((cfg.get("ingest", {}).get("companies_house", {}) or {}).get("home_areas", ["G", "PA"]))
    if (rec.get("company_number") and place_pc and area(place_pc) in SCOTTISH_AREAS
            and location_agreement(place_pc, rec.get("postcode"), rec["company_number"]) == "scotland"
            and area(rec.get("postcode")) not in home):
        rec["ch_flag"] = "; ".join(filter(None, [rec.get("ch_flag"),
            f"linked company's office is in {outward(rec.get('postcode')) or 'another area'}, "
            f"not near the business — confirm it's the same firm"]))

    # 2) chains: decided before sector, and they override it
    why = chain_reason(rec.get("name"), rec.get("website"), cfg)
    if why:
        rec["sector"] = "national_chain"
        rec["chain_reason"] = why
        # the local branch name never maps to the parent company's record
        if rec.get("company_number"):
            rec.update(UNLINKED)
            rec["ch_flag"] = None
        return rec

    # 3) sector sanity: the business's own name/type beats a SIC code
    hint = (sector_from_name(rec.get("name")) or sector_from_types(place_types)
            or sector_from_website(rec.get("website")))
    current = rec.get("sector") or "unknown"
    if hint and current == "unknown":
        rec["sector"] = hint
    elif hint and hint != current:
        notes.append(f"sector from business name; Companies House record says "
                     f"{current.replace('_', ' ')} — check the match")
        rec["sector"] = hint

    if notes:
        rec["ch_flag"] = "; ".join(filter(None, [rec.get("ch_flag")] + notes))
    return rec


# --- local connections ---------------------------------------------------------
def load_connections(cfg, path=None) -> list:
    """The club-maintained list: one row per business, matched on Companies House
    number or business name. Missing file = no recorded connections."""
    import csv, os
    path = path or (cfg.get("connections") or {}).get("file", "connections.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("match") or "").strip()]


def _norm_biz(n):
    n = re.sub(r"\b(ltd|limited|plc|llp|the|co|services|uk|scotland)\b", "", str(n or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def _same_business(a, b) -> bool:
    """Normalised names equal, or the shorter (2+ words) is contained in the
    longer: "Marbill Coaches" ~ "Marbill Coaches Ltd", not "Algas" ~ "Galgas"."""
    a, b = _norm_biz(a), _norm_biz(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short.split()) >= 2 and f" {short} " in f" {long_} "


def apply_connections(rec: dict, cfg: dict, table: list) -> dict:
    """Set connection_kind / connection_note / connection_strength on a record,
    taking the strongest recorded link. Sources, strongest first:
    the club's list, a warm intro, a director based in a local district."""
    st = (cfg.get("connections") or {}).get("strength", {})
    best = (0.0, None, None)
    cn = str(rec.get("company_number") or "").upper()
    nm = _norm_biz(rec.get("name"))
    for row in table:
        key = row["match"].strip()
        hit = (cn and key.upper() == cn) or _same_business(key, rec.get("name"))
        if hit:
            kind = (row.get("kind") or "club_recorded").strip()
            note = (row.get("note") or "").strip() or "recorded by the club"
            if kind == "not_a_fit":       # the club's call overrides any link
                rec.update(connection_strength=0.0, connection_kind=kind, connection_note=note)
                return rec
            best = max(best, (st.get(kind, st.get("club_recorded", 1.0)), kind, note))
    warm = rec.get("warm_intro")
    if isinstance(warm, str) and warm.strip():
        best = max(best, (st.get("warm_intro", 0.9), "warm_intro", f"warm path: {warm.strip()}"))
    if (rec.get("directors_local") or 0) > 0:
        n, tot = rec["directors_local"], rec.get("directors_total") or rec["directors_local"]
        best = max(best, (st.get("local_director", 0.75), "local_director",
                          f"{n} of {tot} active director(s) based locally (Companies House)"))
    rec["connection_strength"], rec["connection_kind"], rec["connection_note"] = best
    return rec


def within_radius(rec: dict, cfg: dict) -> bool:
    max_km = (cfg.get("ingest", {}).get("places", {}).get("max_distance_km"))
    d = rec.get("distance_km")
    return max_km is None or d is None or d <= max_km


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open("sponsor_config.yaml"))
    ok = 0
    def check(label, got, want):
        global ok
        flag = "ok " if got == want else "FAIL"
        ok += got == want
        print(f"  [{flag}] {label}: {got!r}" + ("" if got == want else f"  (wanted {want!r})"))
        return got == want

    print("data_checks — offline checks\n" + "=" * 44)
    check("postcode parse", postcode_of("129 Glasgow Rd, Dumbarton G82 1RQ, UK"), "G82 1RQ")
    check("same district", location_agreement("G82 1RQ", "G82 2BA", "12345678"), "district")
    check("Scottish number, Glasgow office", location_agreement("G82 1RQ", "G2 7DA", "SC082975"), "scotland")
    check("English co, Welsh office", location_agreement("G82 1RQ", "SA20 0AA", "00205581"), "elsewhere")
    check("English co, Scottish office", location_agreement("G82 1RQ", "G82 4AD", "05746890"), "district")

    r = reconcile(dict(name="Castle Garage", address="129 Glasgow Rd, Dumbarton G82 1RQ, UK",
                       sector="motor_dealers_garages", company_number="00205581",
                       postcode="SA20 0AA", company_age=101, size_band="small"), cfg)
    check("wrong-place match unlinked", r["company_number"], None)
    check("…sector recovered from name", r["sector"], "motor_dealers_garages")

    r = reconcile(dict(name="Toolstation Dumbarton", address="Birch Rd, Dumbarton G82 2RE, UK",
                       website="https://www.toolstation.com/?utm_source=google",
                       sector="independent_retail", company_number="08096783",
                       postcode="NN5 7UA"), cfg)
    check("chain by domain", r["sector"], "national_chain")

    r = reconcile(dict(name="B & M Builders", address="Dumbarton G82 2QQ", sector="unknown",
                       website=None, company_number=None, postcode=None), cfg)
    check("'B & M Builders' is NOT B&M stores", r["sector"], "building_construction_trades")

    r = reconcile(dict(name="Energie Fitness Dumbarton", address="Dumbarton G82 1LL",
                       sector="pubs_bars_hospitality", website="https://www.energiefitness.com/gym/dumbarton",
                       company_number="SC416427", postcode="FK8 1AY"), cfg)
    check("franchise caught", r["sector"], "national_chain")

    r = reconcile(dict(name="S1 Auto Services", address="Dumbarton G82 1JB", website=None,
                       sector="local_professional_services", company_number="SC581533",
                       postcode="G74 4BF"), cfg)
    check("SIC/name conflict -> name wins", r["sector"], "motor_dealers_garages")
    check("…and is flagged", "check the match" in (r.get("ch_flag") or ""), True)

    check("sector from website host", sector_from_website("http://www.adairssolicitors.com/"),
          "local_professional_services")
    tbl = [{"match": "SC071529", "kind": "current_sponsor", "note": "club says so"}]
    r = apply_connections(dict(name="Marbill Coach Services Ltd", company_number="SC071529"), cfg, tbl)
    check("club-recorded connection", (r["connection_kind"], r["connection_strength"]), ("current_sponsor", 1.0))
    r = apply_connections(dict(name="X", directors_local=1, directors_total=2), cfg, [])
    check("local director connection", r["connection_kind"], "local_director")
    r = reconcile(dict(name="Lindsays", address="Stirling Rd, Dumbarton G82 2TB, UK",
                       website="http://www.lindsaysvauxhall.co.uk/", sector="unknown",
                       company_number="SO307055", postcode="EH3 8HE"), cfg)
    check("far-office Scottish match flagged", "confirm it's the same firm" in (r.get("ch_flag") or ""), True)
    check("radius filter", within_radius({"distance_km": 53.7}, cfg), False)
    check("…but 21 km is kept", within_radius({"distance_km": 21.3}, cfg), True)
    print(f"\n{ok} checks passed")
