#!/usr/bin/env python3
"""
club_sponsors.py  —  who sponsors Scottish football clubs, and since when
=========================================================================
Collects the sponsor lists that clubs publish about themselves, now and over
the last five seasons, for the clubs in clubs.yaml (SPFL, Highland, Lowland and
West of Scotland leagues).

For each club:
  1. WEBSITE   from clubs.yaml, else the club's official website on Wikidata
               (the structured data behind Wikipedia). Facebook-only clubs are
               reported as such and skipped.
  2. PAGES     the sponsor / partner / commercial pages linked from the
               homepage (or listed in clubs.yaml). robots.txt is honoured.
  3. CURRENT   each page as it is today        -> season now, "club page"
  4. ARCHIVE   one Internet Archive snapshot per season back to 2021/22
               (free Wayback CDX API)          -> that season, "archived page"
  5. NAMES     sponsor names pulled from each page. With an API key the AI
               model reads the page; without, the logos' alt text and outbound
               links are used. EITHER WAY a name is kept only if it appears
               word-for-word on the page (text, logo alt/title, or link), so
               every row is the club's own record, never a guess.

Every fetched page is cached (club_cache/), so a quarterly re-run only fetches
what's new. A coverage report says, club by club, what was found and what
wasn't, so gaps can be fixed in clubs.yaml (add `website` / `sponsor_pages`, or
`skip: true`).

    python club_sponsors.py                    offline fixture check
    python club_sponsors.py --live             all clubs (cached after the first run)
    python club_sponsors.py --live --near      just the clubs near Dumbarton
    python club_sponsors.py --live --no-ai     names from logos/links only (no API cost)
    python club_sponsors.py --plan             what a live run will fetch, nothing fetched

Results go into sponsor.db (club_sponsors, club_coverage) and
club_coverage.csv. Matching them to businesses is match_sponsors.py.
"""
from __future__ import annotations
import argparse
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse

import yaml

UA = "DumbartonFC-SponsorResearch/1.0 (club sponsorship research; one request per page, cached)"
CACHE_DIR = os.environ.get("CLUB_CACHE_DIR", "club_cache")
FIRST_SEASON = 2021                      # 2021/22 onwards: the agreed five-year look-back
PAGE_WORDS = re.compile(r"sponsor|partner|commercial|supporter|advertis|hospitality", re.I)
NOT_PAGES = re.compile(r"\.(pdf|jpe?g|png|gif|zip|docx?)($|\?)|mailto:|tel:|/shop|basket|cart|login", re.I)
SOCIAL = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "tiktok.com",
          "linkedin.com", "flickr.com", "wa.me", "whatsapp.com", "threads.net")
PLATFORM = SOCIAL + ("google.", "goo.gl", "apple.com", "spfl.co.uk", "scottishfa.co.uk", "wosfl.co.uk",
                     "highlandfootballleague.com", "lowlandfootballleague.co.uk", "pitchero.com",
                     "wordpress.", "wix.com", "squarespace.com", "w3.org", "cloudflare", "gstatic.com",
                     "jquery", "bootstrapcdn", "fontawesome", "doubleclick", "addtoany", "clubshop",
                     "web.archive.org", "archive.org", "eventbrite", "ticketmaster", "fanbase", "gofundme",
                     "justgiving", "paypal", "stripe.com")
GENERIC = re.compile(r"^(logo|image|img|banner|sponsor|sponsors|partner|partners|our sponsors|"
                     r"our partners|official partner|click here|read more|visit website|home|"
                     r"menu|search|close|next|previous|back|untitled|photo|picture|facebook|twitter|x|instagram|"
                     r"youtube|tiktok|linkedin|whatsapp|\d+)$", re.I)
SEASON_MONTHS = (11, 12, 10, 1, 2, 3, 9, 4, 5)   # snapshot preference within a season


def season_of(d: date) -> str:
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y}/{str(y + 1)[-2:]}"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(s: str) -> str:
    s = htmllib.unescape(str(s or "")).lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def host(url: str) -> str:
    h = (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


# ---------------------------------------------------------------------------
# PAGE PARSING  (pure, testable)
# ---------------------------------------------------------------------------
def evidence(page_html: str, page_url: str) -> dict:
    """What a sponsor page says, in the forms sponsors usually appear:
    visible text, logo alt/title text, and outbound links (with their text)."""
    from lxml import html as lh
    try:
        doc = lh.fromstring(page_html)
    except Exception:
        return {"text": "", "items": [], "links": []}
    for bad in doc.xpath("//script|//style|//noscript|//header//nav|//footer//nav"):
        bad.drop_tree()
    site = host(page_url)
    items, links = [], []
    for img in doc.xpath("//img"):
        a = next(iter(img.xpath("ancestor::a[1]")), None)
        to = host(urljoin(page_url, a.get("href"))) if a is not None and a.get("href") else ""
        if to and any(p in to for p in PLATFORM):
            continue                               # social / platform badges, not sponsors
        for attr in ("alt", "title"):
            v = (img.get(attr) or "").strip()
            if v and not GENERIC.match(norm(v)) and len(v) <= 80:
                items.append(v)
        if a is not None and a.get("href"):
            links.append((urljoin(page_url, a.get("href")), (img.get("alt") or "").strip()))
    for a in doc.xpath("//a[@href]"):
        href = urljoin(page_url, a.get("href"))
        h = host(href)
        if h and h != site and not h.endswith("." + site):
            t = " ".join(a.text_content().split())[:80]
            links.append((href, t or (a.get("title") or "").strip()))
    outbound = []
    for href, t in links:
        h = host(href)
        if h and h != site and not any(p in h for p in PLATFORM):
            outbound.append({"host": h, "text": t})
    text = " ".join(doc.text_content().split())
    return {"text": text[:12000], "items": sorted(set(items)), "links": outbound}


def sponsor_pages(home_html: str, home_url: str, limit=3) -> list[str]:
    """Same-site links whose address or wording says sponsors/partners/commercial."""
    from lxml import html as lh
    try:
        doc = lh.fromstring(home_html)
    except Exception:
        return []
    site, found = host(home_url), {}
    for a in doc.xpath("//a[@href]"):
        href = urljoin(home_url, a.get("href")).split("#")[0]
        if host(href) != site or NOT_PAGES.search(href):
            continue
        label = " ".join(a.text_content().split())
        hit_url, hit_txt = bool(PAGE_WORDS.search(urlparse(href).path)), bool(PAGE_WORDS.search(label))
        if hit_url or hit_txt:
            # sponsors/partners pages first, then commercial/hospitality
            rank = (0 if re.search(r"sponsor|partner", href + " " + label, re.I) else 1, len(href))
            found[href] = min(found.get(href, rank), rank)
    return [u for u, _ in sorted(found.items(), key=lambda kv: kv[1])][:limit]


def heuristic_names(ev: dict, club_name: str) -> list[dict]:
    """No-AI route: logo alt/title text and outbound links with real names."""
    club = norm(club_name)
    out, seen = [], {}
    def add(name, dom=None):
        n = norm(name)
        if n in seen:                              # same sponsor via its logo link: keep the domain
            if dom and not seen[n].get("domain"):
                seen[n]["domain"] = dom
            return
        if not n or len(n) < 3 or GENERIC.match(n) or club in n:
            return
        if re.search(r"\b(logo|png|jpg|jpeg|svg|webp|img|image|banner)\b", n) and len(n.split()) <= 2:
            return
        item = {"name": re.sub(r"\s*(logo|Logo|LOGO)$", "", name).strip(), "domain": dom}
        seen[n] = item
        out.append(item)
    for i in ev["items"]:
        add(re.sub(r"\s*(logo|Logo|LOGO)$", "", i).strip())
    for l in ev["links"]:
        if l["text"]:
            add(re.sub(r"\s*(logo|Logo|LOGO)$", "", l["text"]).strip(), l["host"])
    return out


def verify(cands: list[dict], ev: dict) -> list[dict]:
    """Keep a name only if it appears word-for-word on the page."""
    hay = " " + norm(ev["text"] + " " + " ".join(ev["items"]) + " " +
                     " ".join(l["text"] for l in ev["links"])) + " "
    hosts = {l["host"] for l in ev["links"]}
    kept = []
    for c in cands:
        n = norm(c.get("name"))
        if n and f" {n} " in hay:
            dom = (c.get("domain") or "").lower().removeprefix("www.") or None
            kept.append({"name": c["name"].strip(), "domain": dom if dom in hosts else None})
    return kept


# ---------------------------------------------------------------------------
# AI READING  (one bounded call per unique page)
# ---------------------------------------------------------------------------
SYSTEM = ("You extract the sponsors and commercial partners that a football club lists on its own web page. "
          "Return ONLY JSON: {\"sponsors\": [{\"name\": str, \"domain\": str|null}]}. Use each name exactly as "
          "written on the page. Include businesses and organisations shown as sponsors, partners, kit, "
          "match, ball, board or player sponsors. Exclude the club itself, leagues, governing bodies, "
          "kit suppliers' generic ads, social networks, website builders and navigation text. "
          "If the page lists no sponsors, return {\"sponsors\": []}.")


def ai_names(ev: dict, club_name: str, api_key=None, model=None, tries=4) -> list[dict] | None:
    import requests
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    model = model or os.environ.get("SPONSOR_LLM_MODEL", "claude-haiku-5-5")
    prompt = (f"Club: {club_name}\n\nLogo alt/title text on the page:\n" + "\n".join(ev["items"][:150]) +
              "\n\nOutbound links (host | link text):\n" +
              "\n".join(f"{l['host']} | {l['text']}" for l in ev["links"][:150]) +
              "\n\nPage text:\n" + ev["text"][:6000])
    for attempt in range(tries):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": model, "max_tokens": 1500, "system": SYSTEM,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(int(r.headers.get("retry-after") or 0) or 3 * 2 ** attempt)
                continue
            if r.status_code in (400, 401, 403, 404):
                raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
            text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
            return list(json.loads(text).get("sponsors") or [])
        except (json.JSONDecodeError, requests.RequestException):
            time.sleep(3 * 2 ** attempt)
    return None                                   # fall back to the heuristic for this page


# ---------------------------------------------------------------------------
# FETCHING  (polite, cached; fixture mode never touches the network)
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, mode="fixture", delay=1.5):
        self.mode, self.delay, self.last, self.robots = mode, delay, {}, {}
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _cache(self, url):
        return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest()[:20] + ".html")

    def allowed(self, url) -> bool:
        if self.mode == "fixture" or "web.archive.org" in url or "wikidata.org" in url:
            return True
        from urllib import robotparser
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if base not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                txt = self.get(base + "/robots.txt", check_robots=False, cache=False) or ""
                rp.parse(txt.splitlines())
            except Exception:
                rp.parse([])
            self.robots[base] = rp
        return self.robots[base].can_fetch(UA, url)

    def get(self, url, check_robots=True, cache=True, accept="text/html") -> str | None:
        if self.mode == "fixture":
            return FIXTURE.get(url)
        path = self._cache(url)
        if cache and os.path.exists(path):
            return open(path, encoding="utf-8", errors="replace").read()
        if check_robots and not self.allowed(url):
            return None
        import requests
        h = host(url)
        wait = self.delay - (time.monotonic() - self.last.get(h, 0))
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self.last[h] = time.monotonic()
            try:
                r = requests.get(url, headers={"User-Agent": UA, "Accept": accept}, timeout=30)
            except requests.RequestException:
                time.sleep(2 * 2 ** attempt)
                continue
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("retry-after") or 0) or 5 * 2 ** attempt)
                continue
            if r.status_code != 200:
                return None
            if cache:
                open(path, "w", encoding="utf-8").write(r.text)
            return r.text
        return None


def resolve_website(club: dict, f: Fetcher) -> str | None:
    """clubs.yaml first, else Wikidata's official website (P856) for the club's
    English Wikipedia article (redirects followed)."""
    if club.get("website"):
        return club["website"]
    from urllib.parse import quote
    url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&sites=enwiki&redirects=yes"
           f"&props=claims&format=json&titles={quote(club['wiki'])}")
    raw = f.get(url, accept="application/json")
    try:
        ent = next(iter(json.loads(raw)["entities"].values()))
        claims = ent.get("claims", {}).get("P856", [])
        # prefer a current (non-deprecated) value
        claims = sorted(claims, key=lambda c: c.get("rank") != "preferred")
        for c in claims:
            if c.get("rank") != "deprecated":
                return c["mainsnak"]["datavalue"]["value"]
    except Exception:
        return None
    return None


def archived_pages(site: str, f: Fetcher, limit=3) -> list[str]:
    """Sponsor/partner pages the Internet Archive holds for this site since 2021,
    including ones a redesign has since moved or removed."""
    cdx = ("https://web.archive.org/cdx/search/cdx?output=json&fl=original&collapse=urlkey"
           f"&filter=statuscode:200&filter=original:.*(sponsor|partner).*&from={FIRST_SEASON}07&limit=40"
           f"&url={host(site)}/*")
    try:
        rows = [r[0] for r in json.loads(f.get(cdx, accept="application/json"))[1:]]
    except Exception:
        return []
    rows = [u.split("?")[0].split("#")[0] for u in rows if not NOT_PAGES.search(u)]
    return sorted(set(rows), key=lambda u: (u.count("/"), len(u)))[:limit]


def snapshots(page_url: str, f: Fetcher, today: date) -> dict[str, str]:
    """One Internet Archive snapshot per season (2021/22 to last season):
    season -> raw-page URL. Prefers mid-season months."""
    cdx = ("https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp,original"
           f"&filter=statuscode:200&collapse=timestamp:6&from={FIRST_SEASON}07&url={page_url}")
    raw = f.get(cdx, accept="application/json")
    try:
        rows = json.loads(raw)[1:]
    except Exception:
        return {}
    by_season = {}
    this = season_of(today)
    for ts, orig in rows:
        d = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        s = season_of(d)
        if s == this:
            continue                               # the live page covers this season
        pref = SEASON_MONTHS.index(d.month) if d.month in SEASON_MONTHS else 99
        if s not in by_season or pref < by_season[s][0]:
            by_season[s] = (pref, f"https://web.archive.org/web/{ts}id_/{orig}")
    return {s: u for s, (_, u) in sorted(by_season.items())}


# ---------------------------------------------------------------------------
# COLLECT
# ---------------------------------------------------------------------------
def names_from(page_html, page_url, club_name, use_ai, seen_pages):
    ev = evidence(page_html, page_url)
    key = hashlib.sha1((ev["text"][:4000] + "|".join(ev["items"])).encode()).hexdigest()
    if key in seen_pages:                          # identical snapshot: same answer, no new call
        return seen_pages[key]
    cands, method = None, "logos"
    if use_ai:
        cands = ai_names(ev, club_name)
        method = "ai" if cands is not None else "logos"
    if cands is None:
        cands = heuristic_names(ev, club_name)
    out = (verify(cands, ev), method)
    seen_pages[key] = out
    return out


def collect(clubs, mode="fixture", use_ai=False, today=None, log=print):
    today = today or date.today()
    f = Fetcher(mode)
    rows, coverage, seen_pages = [], [], {}
    this = season_of(today)
    for i, c in enumerate(clubs, 1):
        cov = dict(club=c["name"], league=c["league"], tier=c["tier"], near=bool(c.get("near")),
                   website=None, status="", pages=0, current_sponsors=0, archive_seasons=0, rows=0)
        if c.get("skip"):
            cov["status"] = "skipped in clubs.yaml"
            coverage.append(cov)
            continue
        site = resolve_website(c, f)
        cov["website"] = site
        if not site:
            cov["status"] = "no website found (add one in clubs.yaml, or skip: true)"
            coverage.append(cov)
            continue
        if any(s in host(site) for s in SOCIAL):
            cov["status"] = "Facebook / social only: nothing to collect"
            coverage.append(cov)
            continue
        pages = list(c.get("sponsor_pages") or [])
        if not pages:
            home = f.get(site)
            if home is None:
                cov["status"] = "website unreachable or blocked by robots.txt"
                coverage.append(cov)
                continue
            pages = sponsor_pages(home, site)
        cov["pages"] = len(pages)
        if not pages and not archived_pages(site, f):
            cov["status"] = "no sponsor page linked from the homepage (add sponsor_pages in clubs.yaml)"
            coverage.append(cov)
            continue
        club_rows = []
        old_pages = list(dict.fromkeys(pages + [u for u in archived_pages(site, f)
                                                if host(u) == host(site) and u.rstrip("/") not in
                                                {p.rstrip("/") for p in pages}]))
        for pg in old_pages:
            live = f.get(pg) if pg in pages else None
            if live:
                names, method = names_from(live, pg, c["name"], use_ai, seen_pages)
                cov["current_sponsors"] += len(names)
                club_rows += [dict(season=this, source_type="club_page", source_url=pg, method=method, **n)
                              for n in names]
            for s, snap in snapshots(pg, f, today).items():
                old = f.get(snap)
                if not old:
                    continue
                names, method = names_from(old, pg, c["name"], use_ai, seen_pages)
                club_rows += [dict(season=s, source_type="archived_page", source_url=snap, method=method, **n)
                              for n in names]
        # one row per sponsor per season (a sponsor on two pages counts once)
        uniq = {}
        for r in club_rows:
            uniq.setdefault((norm(r["name"]), r["season"]), r)
        for r in uniq.values():
            rows.append(dict(club=c["name"], league=c["league"], tier=c["tier"], near=bool(c.get("near")),
                             sponsor_name=r["name"], sponsor_domain=r["domain"], season=r["season"],
                             source_type=r["source_type"], source_url=r["source_url"], method=r["method"],
                             seen_at=now()))
        cov["archive_seasons"] = len({r["season"] for r in uniq.values() if r["source_type"] == "archived_page"})
        cov["rows"] = len(uniq)
        cov["status"] = "ok" if uniq else "sponsor page found but no names read from it"
        coverage.append(cov)
        if mode != "fixture" and (i % 10 == 0 or i == len(clubs)):
            log(f"  clubs: {i}/{len(clubs)}  ({len(rows)} sponsor-season rows so far)")
    return rows, coverage


def save(conn, rows, coverage):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS club_sponsors (
      club TEXT, league TEXT, tier INTEGER, near INTEGER, sponsor_name TEXT, sponsor_domain TEXT,
      season TEXT, source_type TEXT, source_url TEXT, method TEXT, seen_at TEXT,
      PRIMARY KEY (club, sponsor_name, season));
    CREATE TABLE IF NOT EXISTS club_coverage (
      club TEXT PRIMARY KEY, league TEXT, tier INTEGER, near INTEGER, website TEXT, status TEXT,
      pages INTEGER, current_sponsors INTEGER, archive_seasons INTEGER, rows INTEGER, checked_at TEXT);
    """)
    # accumulate: a sponsor seen once stays on record; a re-run refreshes it
    conn.executemany(
        "INSERT INTO club_sponsors VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(club, sponsor_name, season) "
        "DO UPDATE SET sponsor_domain=excluded.sponsor_domain, source_type=excluded.source_type, "
        "source_url=excluded.source_url, method=excluded.method, seen_at=excluded.seen_at",
        [(r["club"], r["league"], r["tier"], int(r["near"]), r["sponsor_name"], r["sponsor_domain"],
          r["season"], r["source_type"], r["source_url"], r["method"], r["seen_at"]) for r in rows])
    conn.executemany(
        "INSERT OR REPLACE INTO club_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(c["club"], c["league"], c["tier"], int(c["near"]), c["website"], c["status"], c["pages"],
          c["current_sponsors"], c["archive_seasons"], c["rows"], now()) for c in coverage])
    conn.commit()


def write_coverage_csv(coverage, path="club_coverage.csv"):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(coverage[0]))
        w.writeheader()
        w.writerows(coverage)


def summarise(rows, coverage, log=print):
    from collections import Counter
    st = Counter("ok" if c["status"] == "ok" else c["status"].split(" (")[0].split(":")[0] for c in coverage)
    log(f"  clubs: {len(coverage)}  |  " + ", ".join(f"{k}: {v}" for k, v in st.most_common()))
    log(f"  sponsor-season rows: {len(rows)}  |  distinct sponsors: {len({norm(r['sponsor_name']) for r in rows})}"
        f"  |  seasons: {', '.join(sorted({r['season'] for r in rows}))}")


def load_clubs(path="clubs.yaml", near=False, tier_max=None):
    clubs = yaml.safe_load(open(path, encoding="utf-8"))["clubs"]
    if near:
        clubs = [c for c in clubs if c.get("near")]
    if tier_max:
        clubs = [c for c in clubs if c["tier"] <= tier_max]
    return clubs


# ---------------------------------------------------------------------------
# OFFLINE FIXTURE  (real-shaped pages: a logo grid, a text list, an archive)
# ---------------------------------------------------------------------------
_HOME = """<html><body><nav><a href="/news">News</a><a href="/club/commercial">Commercial</a>
<a href="/sponsors">Our Sponsors</a><a href="https://facebook.com/x">Facebook</a></nav></body></html>"""
_SPONSORS_NOW = """<html><body><h1>Our Sponsors</h1><div class="grid">
<a href="https://www.marbillcoaches.example/"><img src="m.png" alt="Marbill Coaches"></a>
<a href="https://jjrprint.example"><img src="j.png" alt="JJR Print &amp; Design logo"></a>
<a href="https://www.algas.example"><img src="a.png" alt="logo"></a>
<a href="https://twitter.com/club"><img src="t.png" alt="Twitter"></a></div>
<p>Thanks to Algas for their continued support.</p></body></html>"""
_SPONSORS_2023 = """<html><body><h1>Club Partners</h1>
<a href="https://ngage.example"><img alt="Ngage Technology"></a>
<a href="https://pitchteamwear.example"><img alt="Pitch Teamwear"></a></body></html>"""
_OTHER_CLUB = """<html><body><h2>Partners</h2><ul><li>Leven Valley Joinery</li><li>The Anchor Inn</li></ul>
<a href="https://levenvalley.example">Leven Valley Joinery</a></body></html>"""
FIXTURE = {
    "https://www.dumbartonfootballclub.com/club/commercial": _SPONSORS_NOW,
    "https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp,original&filter=statuscode:200"
    "&collapse=timestamp:6&from=202107&url=https://www.dumbartonfootballclub.com/club/commercial":
        json.dumps([["timestamp", "original"],
                    ["20231105120000", "https://www.dumbartonfootballclub.com/club/commercial"],
                    ["20240310120000", "https://www.dumbartonfootballclub.com/club/commercial"]]),
    "https://web.archive.org/web/20231105120000id_/https://www.dumbartonfootballclub.com/club/commercial":
        _SPONSORS_2023,
    # an old sponsors page that a redesign removed: only the archive has it
    "https://web.archive.org/cdx/search/cdx?output=json&fl=original&collapse=urlkey&filter=statuscode:200"
    "&filter=original:.*(sponsor|partner).*&from=202107&limit=40&url=dumbartonfootballclub.com/*":
        json.dumps([["original"], ["https://www.dumbartonfootballclub.com/sponsors-2022"]]),
    "https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp,original&filter=statuscode:200"
    "&collapse=timestamp:6&from=202107&url=https://www.dumbartonfootballclub.com/sponsors-2022":
        json.dumps([["timestamp", "original"], ["20221120090000", "https://www.dumbartonfootballclub.com/sponsors-2022"]]),
    "https://web.archive.org/web/20221120090000id_/https://www.dumbartonfootballclub.com/sponsors-2022":
        "<html><body><a href='https://yourradio.example'><img alt='YOUR Radio'></a></body></html>",
    "https://www.wikidata.org/w/api.php?action=wbgetentities&sites=enwiki&redirects=yes&props=claims"
    "&format=json&titles=Vale%20of%20Leven%20F.C.":
        json.dumps({"entities": {"Q1": {"claims": {"P856": [{"rank": "normal",
                    "mainsnak": {"datavalue": {"value": "https://valeofleven.example"}}}]}}}}),
    "https://valeofleven.example": _HOME.replace("/club/commercial", "/partners"),
    "https://valeofleven.example/sponsors": _OTHER_CLUB,
    "https://valeofleven.example/partners": _OTHER_CLUB,
}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="collect club sponsor lists")
    ap.add_argument("--live", action="store_true", help="fetch for real (default: offline fixture check)")
    ap.add_argument("--near", action="store_true", help="only clubs marked near: true")
    ap.add_argument("--tier-max", type=int, default=None, help="only clubs at this tier or higher (1 = top)")
    ap.add_argument("--no-ai", action="store_true", help="names from logos and links only")
    ap.add_argument("--plan", action="store_true", help="show what a live run would do")
    a = ap.parse_args()

    if not a.live and not a.plan:
        print("club sponsors — offline fixture checks\n" + "=" * 44)
        assert season_of(date(2026, 10, 19)) == "2026/27" and season_of(date(2024, 3, 1)) == "2023/24"
        assert sponsor_pages(_HOME, "https://club.example") == [
            "https://club.example/sponsors", "https://club.example/club/commercial"]
        clubs = [dict(name="Dumbarton", league="Scottish League Two", tier=4, near=True,
                      website="https://www.dumbartonfootballclub.com",
                      sponsor_pages=["https://www.dumbartonfootballclub.com/club/commercial"]),
                 dict(name="Vale of Leven", league="WoSFL Third Division", tier=9, near=True,
                      wiki="Vale of Leven F.C."),
                 dict(name="Facebook FC", league="x", tier=10, website="https://www.facebook.com/fbfc"),
                 dict(name="Nowhere United", league="x", tier=10, wiki="Nowhere United F.C.")]
        rows, cov = collect(clubs, mode="fixture", today=date(2026, 10, 19))
        got = sorted((r["club"], r["sponsor_name"], r["season"], r["source_type"]) for r in rows)
        for g in got:
            print("  ", g)
        names_now = {r["sponsor_name"] for r in rows if r["club"] == "Dumbarton" and r["season"] == "2026/27"}
        assert names_now == {"Marbill Coaches", "JJR Print & Design"}, names_now   # 'logo'/'Twitter' dropped
        assert {r["sponsor_name"] for r in rows if r["season"] == "2023/24"} == {"Ngage Technology", "Pitch Teamwear"}
        assert {r["sponsor_name"] for r in rows if r["season"] == "2022/23"} == {"YOUR Radio"}   # moved page, via archive
        assert {r["sponsor_name"] for r in rows if r["club"] == "Vale of Leven"} == {"Leven Valley Joinery"}
        st = {c["club"]: c["status"] for c in cov}
        assert st["Facebook FC"].startswith("Facebook") and st["Nowhere United"].startswith("no website")
        # the verbatim rule: an invented name is dropped, a real one kept
        ev = evidence(_SPONSORS_NOW, "https://www.dumbartonfootballclub.com/club/commercial")
        assert [v["name"] for v in verify([{"name": "Algas"}, {"name": "Acme Widgets"}], ev)] == ["Algas"]
        print("\n  website from Wikidata, sponsor pages found, archive by season, Facebook-only and "
              "missing sites reported, invented names dropped ✓")
        sys.exit(0)

    clubs = load_clubs(near=a.near, tier_max=a.tier_max)
    use_ai = (not a.no_ai) and bool(os.environ.get("ANTHROPIC_API_KEY"))
    if a.plan:
        cached = len(os.listdir(CACHE_DIR)) if os.path.isdir(CACHE_DIR) else 0
        print(f"clubs: {len(clubs)} ({sum(1 for c in clubs if c.get('near'))} near Dumbarton)")
        print(f"fetches: roughly {len(clubs) * 3} pages now + up to {len(clubs) * 2 * 5} archive snapshots "
              f"(5 seasons x ~2 pages); {cached} pages already cached")
        print(f"time: about {len(clubs) * 13 * 1.5 / 60:.0f} minutes on a first run, a few minutes once cached")
        print(f"names: {'AI model, one call per unique page' if use_ai else 'logos and links only (no API cost)'}"
              + ("" if use_ai or a.no_ai else "  [no ANTHROPIC_API_KEY set]"))
        sys.exit(0)

    import sponsor_db as db
    print(f"collecting sponsors for {len(clubs)} clubs  |  names: {'AI' if use_ai else 'logos/links'}")
    rows, cov = collect(clubs, mode="live", use_ai=use_ai)
    conn = db.connect(); db.init_db(conn)
    save(conn, rows, cov)
    write_coverage_csv(cov)
    summarise(rows, cov)
    print("  wrote club_coverage.csv (club by club: what was found and what to fix in clubs.yaml)")
