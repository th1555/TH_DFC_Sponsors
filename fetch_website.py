#!/usr/bin/env python3
"""
fetch_website.py  —  pull a business's homepage text for the soft-feature layer
===============================================================================
soft_features.py reads a business's own website copy to judge community affinity,
brand fit and CSR. This grabs that copy: fetch the homepage, strip the HTML to
visible text, truncate to a sane size. Empty/blocked pages return "" — which the
soft-feature layer already handles as low confidence (pulled to neutral).

Be a polite citizen: a real User-Agent, a short timeout, and in production honour
robots.txt and cache results (don't re-fetch every run). Homepage text only —
we don't crawl.

  mode="fixture" (default) — canned copy keyed by domain; offline.
  mode="http"             — real fetch.
"""
from __future__ import annotations
import re

UA = "DumbartonFC-SponsorResearch/1.0 (contact: tom@dumbartonfc.example)"
MAX_CHARS = 1500


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|head).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_http(url, timeout=15) -> str:
    import requests
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return ""
        return strip_html(r.text)[:MAX_CHARS]
    except Exception:
        return ""                           # unreachable page -> no text, low confidence


# canned copy keyed by a domain fragment, aligned to the Places/CH fixtures
_FIX = {
    "marbill": "Marbill Coaches is a family-run coach and haulage firm based in the "
               "Vale of Leven, proudly serving West Dunbartonshire since 1987. We support "
               "local grassroots sport and community groups across Dumbarton and have "
               "sponsored youth teams in the area for over a decade, including Vale of Leven "
               "Boys Club and the Dumbarton FC Under 14s.",
    "anchor": "The Anchor Inn is a traditional community pub in the heart of Dumbarton — "
              "a proud local on matchdays, with quiz nights and charity fundraisers for "
              "town causes throughout the year.",
    "bonhill": "An independent, family-owned funeral director rooted in the local community "
               "for three generations. We are proud of our ties to the town and regularly "
               "support local charity fundraising.",
    "levenvalley": "Local independent joiners and builders, established in Dumbarton, trusted "
                   "by families across the district for quality workmanship.",
    "dumbartonmotors": "Independent local garage, MOT centre and used-car dealer, owner-run "
                       "and serving drivers in the town for over two decades.",
    "bytebargains": "ByteBargains ships electronics nationwide, delivered to your door. "
                    "UK-wide next-day delivery on thousands of online-only deals.",
}


def fetch_fixture(url) -> str:
    if not url:
        return ""
    for key, copy in _FIX.items():
        if key in url.lower():
            return copy
    return ""


def fetch(url, mode="fixture") -> str:
    return fetch_fixture(url) if mode == "fixture" else fetch_http(url)


if __name__ == "__main__":
    print("website fetch — offline fixture\n" + "=" * 40)
    for u in ["https://marbillcoaches.example", "https://bytebargains.example",
              "https://anchorinn.example", None]:
        t = fetch(u, mode="fixture")
        print(f"\n{u}:\n  {('(no text)' if not t else t[:110] + '…')}")
    # strip_html sanity
    s = strip_html("<html><head><style>x{}</style></head><body>Hello <b>town</b>!</body></html>")
    assert s == "Hello town !", repr(s)
    print("\nstrip_html sanity ✓")
