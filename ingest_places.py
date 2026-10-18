#!/usr/bin/env python3
"""
ingest_places.py  —  find candidate businesses near the ground (Google Places)
==============================================================================
The candidate-discovery step: run a set of sector seed-queries (from config),
location-biased to the stadium, and return a deduped list of nearby businesses
with name, location, website, and distance-to-ground (Haversine). Those names
then go to Companies House for enrichment.

Uses the Places API (new) Text Search. Needs GOOGLE_PLACES_API_KEY in env for the
real path; the fixture path runs offline with canned Dumbarton-area results.

  mode="fixture" (default) — canned places around the ground; offline.
  mode="api"              — real Places Text Search calls.

python ingest_places.py   runs the offline discovery + distance checks.
"""
from __future__ import annotations
import os
import math

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ("places.id,places.displayName,places.formattedAddress,"
              "places.location,places.types,places.businessStatus,places.websiteUri,"
              "places.rating,places.userRatingCount")
# rating / userRatingCount may bill at a higher Places SKU than the basic fields


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# REAL API
# ---------------------------------------------------------------------------
def text_search_api(query, lat, lng, radius_m, n, api_key=None):
    import requests
    api_key = api_key or os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise RuntimeError("no GOOGLE_PLACES_API_KEY — use mode='fixture' to run offline")
    r = requests.post(TEXT_SEARCH_URL,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": api_key,
                 "X-Goog-FieldMask": FIELD_MASK},
        json={"textQuery": query,
              "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                          "radius": float(radius_m)}},
              "maxResultCount": n}, timeout=30)
    r.raise_for_status()
    return r.json().get("places", [])


def _parse_place(p: dict) -> dict:
    loc = p.get("location") or {}
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text"),
        "address": p.get("formattedAddress"),
        "lat": loc.get("latitude"), "lng": loc.get("longitude"),
        "types": p.get("types") or [],
        "website": p.get("websiteUri"),
        "business_status": p.get("businessStatus"),
        "google_rating": p.get("rating"),
        "google_reviews": p.get("userRatingCount"),
    }


# ---------------------------------------------------------------------------
# OFFLINE FIXTURE  (places around the ground, names aligned to the CH fixtures)
# ---------------------------------------------------------------------------
_FIX_PLACES = [
    dict(id="p1", displayName={"text": "Marbill Coaches"}, formattedAddress="Dumbarton G82 1AB",
         location={"latitude": 55.9405, "longitude": -4.5600}, types=["travel_agency", "point_of_interest"],
         websiteUri="https://marbillcoaches.example", businessStatus="OPERATIONAL"),
    dict(id="p2", displayName={"text": "The Anchor Inn"}, formattedAddress="Dumbarton G82 1QS",
         location={"latitude": 55.9372, "longitude": -4.5631}, types=["bar", "restaurant"],
         websiteUri="https://anchorinn.example", businessStatus="OPERATIONAL",
         rating=4.5, userRatingCount=212),
    dict(id="p3", displayName={"text": "Bonhill Funeralcare"}, formattedAddress="Alexandria G83 9JX",
         location={"latitude": 55.9780, "longitude": -4.5830}, types=["funeral_home"],
         websiteUri="https://bonhillfuneralcare.example", businessStatus="OPERATIONAL"),
    dict(id="p4", displayName={"text": "Leven Valley Joinery"}, formattedAddress="Dumbarton G82 3PD",
         location={"latitude": 55.9520, "longitude": -4.5700}, types=["general_contractor"],
         websiteUri="https://levenvalleyjoinery.example", businessStatus="OPERATIONAL"),
    dict(id="p5", displayName={"text": "Dumbarton Motors"}, formattedAddress="Dumbarton G82 2RX",
         location={"latitude": 55.9430, "longitude": -4.5520}, types=["car_dealer", "car_repair"],
         websiteUri="https://dumbartonmotors.example", businessStatus="OPERATIONAL"),
    dict(id="p6", displayName={"text": "ByteBargains Online"}, formattedAddress="Clydebank G81 1AA",
         location={"latitude": 55.9000, "longitude": -4.4000}, types=["electronics_store"],
         websiteUri="https://bytebargains.example", businessStatus="OPERATIONAL"),
    dict(id="p7", displayName={"text": "West End Chippy"}, formattedAddress="Dumbarton G82 1AA",
         location={"latitude": 55.9360, "longitude": -4.5700}, types=["meal_takeaway"],
         websiteUri=None, businessStatus="OPERATIONAL"),
    dict(id="p1", displayName={"text": "Marbill Coaches"}, formattedAddress="Dumbarton G82 1AB",
         location={"latitude": 55.9405, "longitude": -4.5600}, types=["travel_agency"],
         websiteUri="https://marbillcoaches.example", businessStatus="OPERATIONAL"),  # dup -> deduped
]


def text_search_fixture(query, lat, lng, radius_m, n, **_):
    """Crudely 'matches' the query to fixture places by type/name keyword, so the
    offline run behaves like a real multi-query discovery with overlaps."""
    q = query.lower()
    key = {"coach": "marbill", "haulage": "marbill", "pub": "anchor", "bar": "anchor",
           "funeral": "bonhill", "joiner": "leven", "builder": "leven",
           "car": "dumbarton motors", "garage": "dumbarton motors",
           "takeaway": "chippy", "electronic": "bytebargains"}.get(
               next((k for k in ["coach", "haulage", "pub", "bar", "funeral", "joiner",
                                 "builder", "car", "garage", "takeaway", "electronic"]
                     if k in q), ""), None)
    out = [p for p in _FIX_PLACES if key and key in p["displayName"]["text"].lower()]
    return out[:n]


# ---------------------------------------------------------------------------
# DISCOVER — run all seed queries, dedupe, attach distance
# ---------------------------------------------------------------------------
def discover(cfg, mode="fixture", api_key=None) -> list:
    ing = cfg["ingest"]
    lat, lng = ing["stadium"]["lat"], ing["stadium"]["lng"]
    radius, n = ing["places"]["radius_m"], ing["places"]["max_per_query"]
    # Google treats the radius as a preference, not a limit, so enforce it here
    max_km = ing["places"].get("max_distance_km")
    search = text_search_fixture if mode == "fixture" else text_search_api

    seen, out = set(), []
    towns = ing["places"].get("towns") or [None]
    queries = [f"{q} in {t}" if t else q for q in ing["places"]["seed_queries"] for t in towns]
    for q in queries:
        try:
            for raw in search(q, lat, lng, radius, n, api_key=api_key):
                p = _parse_place(raw)
                if not p["place_id"] or p["place_id"] in seen:
                    continue
                seen.add(p["place_id"])
                if p["lat"] is not None:
                    p["distance_km"] = round(haversine_km(lat, lng, p["lat"], p["lng"]), 2)
                if max_km is not None and p.get("distance_km", 0) > max_km:
                    continue                          # outside the catchment
                if p.get("business_status") == "CLOSED_PERMANENTLY":
                    continue                          # no longer trading
                out.append(p)
        except Exception as e:
            print(f"  [places] query {q!r} failed: {e}")
    return out


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open("sponsor_config.yaml"))
    cands = discover(cfg, mode="fixture")
    print("Places discovery — offline fixture\n" + "=" * 52)
    print(f"{len(cands)} unique candidates near {cfg['ingest']['stadium']['name']}:\n")
    for p in sorted(cands, key=lambda x: x.get("distance_km", 99)):
        print(f"  {p['distance_km']:>5} km  {p['name']:<22} "
              f"{'web' if p['website'] else 'no-web':<7} {','.join(p['types'][:2])}")
    # distance sanity: the two closest should be in-town, ByteBargains far
    d = {p["name"]: p.get("distance_km") for p in cands}
    far = d.get('ByteBargains Online')
    print(f"\ndistance check: Anchor Inn {d.get('The Anchor Inn')}km (near), "
          f"ByteBargains {'dropped (outside max_distance_km)' if far is None else str(far) + 'km'}"
          f" — kept {len(cands)} of {len(_FIX_PLACES)} raw rows")
