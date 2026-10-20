# Sons Sponsors · Dumbarton FC sponsor intelligence

A human-in-the-loop tool that ranks local businesses by how well they fit as
sponsors for **Dumbarton FC**, a Scottish League Two club. It sells the club's
real asset — **local affinity, not reach** — and produces a prioritised action
queue the club works top-down. The tool proposes; a person owns every contact.

> **Honest framing.** Most of this is transparent data engineering with a clear
> weighted score. Exactly **one** component uses an LLM, and it's deliberately
> small and bounded: it reads a business's own website text and returns three
> soft judgments, each with a confidence and a justification. It never decides
> the final score — a deterministic engine does that, and every score decomposes
> into its reasons.

---

## How it works

```
  website text + Companies House facts
        │
        ▼
  soft_features.py     three LLM soft judgments (community affinity, brand fit,
                       CSR signal) — each {value, confidence, justification},
                       or an offline keyword stub for development
        │
        ▼
  sponsor_scoring.py   deterministic 0–100 fit score = weighted sum of
                       proximity, sector propensity, stability, ability-to-pay,
                       and the three soft features (low confidence → neutral)
        │
        ▼
  sponsor_db.py        SQLite: businesses + soft_features + scores, with a
                       WALLED human table (outreach_status) a re-score can't touch
        │
        ▼
  export_sponsor_site.py   the club website: one static hub page in the Sons
                           Scouting design, every value tagged by its source
```

`sponsor_pipeline.py` runs the whole chain and builds `sponsor.db`. `outreach.py`
records who has been approached. `sponsor_app.py` (Streamlit) is kept as a local
working view; the club-facing front end is the website.

## Design principles

- **Human-in-the-loop by choice** — the tool prioritises and prepares; a person
  decides and acts. This shapes scope, trust, and liability for a small club.
- **Auditable scores** — every fit score breaks down into per-feature points; no
  black box, no "ask the LLM for 87/100 from nothing."
- **Confidence guardrail** — a thin or missing website yields low confidence, and
  low-confidence soft reads are pulled toward neutral so they can't swing a score.
  A business is never rewarded for community ties its own text doesn't state.
- **The wall** — scores are recomputable and disposable; the human's outreach
  notes are sacred. A data refresh never wipes a note.

## Run it

**Easiest: Google Colab.** Open `dumbarton_sponsor_colab.ipynb` in Colab, add your keys as
Colab Secrets, and run the cells top to bottom. It runs on test data until you set
`GO_LIVE = True`, shows what a live run will do first, and ends with a download of the
website and sponsor list.

Or locally:

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

python sponsor_pipeline.py        # build sponsor.db (offline, demo businesses)
python export_sponsor_site.py     # write site/index.html (+ a lab.html redirect)
```

Open `site/index.html` in a browser. Demo data is labelled as such on the page.

## The club website

Same design, structure and conventions as Sons Scouting, so the two read as one
product: the ranked list with filters, a table and a "map of fit" chart, CSV
download, and a profile for each business showing where its points come from.

Since Phase 2 it's **one hub page** (the old lab page was merged in; `lab.html` now
redirects). Facts and judgments sit side by side, each labelled with where it came from:

| Tag | Meaning |
|---|---|
| **Reported** | a filed or published record (Companies House, the club's own pages) |
| **Estimate** | worked out by us, with the working shown (revenue from ONS averages) |
| **AI reading** | judged by the AI model from the business's own website |
| **Possible lead** | spotted by AI, not yet confirmed by a person; not scored |
| **Not available** | we don't have it, and the page says why |

The headline **Fit score** (out of 100) keeps the two kinds of evidence apart: 56 points
from records (distance or a local connection, sector, trading record, size) and 44 from
the AI website readings. The table bar shows them solid and striped, every profile
breaks them down, and a "Records only" column ranks on facts alone. Employees and
revenue are shown for context and don't change the score. If the readings came from the
offline keyword stand-in instead of the AI model, the page says so at the top.

## Recording outreach

The website is static, so status isn't edited in the browser. It lives in the
walled `outreach_status` table and is changed with `outreach.py`; a pipeline
refresh never touches it.

```bash
python outreach.py set "Rockside Coaches" contacted --owner Tom --note "Emailed 14 Oct"
python outreach.py list
python export_sponsor_site.py     # publish the change
```

Real LLM soft-feature derivation (optional):

```bash
export ANTHROPIC_API_KEY=…        # never commit this
python sponsor_pipeline.py --llm
```

## Real data

The pipeline runs on **synthetic businesses** by default (fully offline), and on
**real businesses** with `--real`. The real-data layer is built:

- **`ingest_places.py`** — Google Places Text Search discovers candidate
  businesses near the ground and computes distance (Haversine).
- **`ingest_companies_house.py`** — the free Companies House API enriches each by
  name → sector, age, size band, accounts/dormant status. Fuzzy name matches that
  don't clear a confidence floor are **flagged, not guessed**.
- **`sic_sectors.py`** — maps raw UK SIC 2007 codes to the 13-sector taxonomy
  (the keystone that makes Companies House legible to the scorer).
- **`fetch_website.py`** — pulls each homepage's text for the soft-feature layer.

Every client has an offline **fixture** mode (so the whole `--real` path runs with
no keys) and a live **API** mode. Because these are official APIs, they don't block
datacenter IPs — the real ingestion can run anywhere, not just locally.

```bash
python sponsor_pipeline.py --real                   # real businesses, offline fixtures
python sponsor_pipeline.py --real --api --llm       # fully live (needs 3 keys below)
```

Live keys (never commit them): `GOOGLE_PLACES_API_KEY`, `COMPANIES_HOUSE_API_KEY`,
`ANTHROPIC_API_KEY`. `sponsor_scoring.score_business()` scores a real enriched
record identically to a synthetic one — that's the seam.

## Data checks

The first real run (Oct 2026) linked many businesses to the wrong Companies House
company (name-only matching), never recognised national chains, and kept results
far outside the search radius. `data_checks.py` now guards all three, in the live
pipeline and in `repair_published_data.py`:

- **Location check on Companies House matches.** A name match must also agree on
  place: same postcode district, or registered (or registered office) in Scotland.
  Dissolved companies are skipped. Wrong-place matches are not linked and are flagged;
  with no confirmed record, company age scores neutral rather than as a new firm.
- **Chains and franchises** are caught by website domain and name (`chains:` in the
  config) and classed `national_chain`.
- **Sector sanity.** If the business's own name, Google type or website disagrees with
  the matched company's SIC code, the name/type wins and the row is flagged.
- **Distance is scored and filtered, not cut.** Discovery covers ~25 km (each search
  runs per town so Glasgow can't crowd out Dumbarton); a 25 km ceiling removes noise.
  Inside it, distance decays the score and the page has a distance filter. Permanently
  closed businesses are dropped.

To fix an already-published dataset without re-calling the APIs (it only removes or
relabels data, never adds it):

```bash
python repair_published_data.py --src index.html --out site   # or an old lab.html
```

A fresh live run (`--real --api --llm`) is still better: it can find the *right*
company where the old run picked the wrong one.

## Local connections

Distance stands in for "local", but some of the best prospects are further out and
still tied to the town. A **recorded connection** counts as local presence: the
proximity part scores `max(distance score, connection strength)`, so weights still
sum to 100 and the profile says what lifted it. Three sources, all recorded facts:

| Source | Strength | Where it comes from |
|---|---:|---|
| Club-recorded (e.g. current sponsor) | 1.0 | `connections.csv`: `match` (company number or business name), `kind`, `note` |
| Warm path | 0.9 | `warm_intro` on the business |
| Director based locally | 0.75 | Companies House officers with a service address in `local_districts` (count only; no names or addresses stored) |

Kinds in `connections.csv`: `current_sponsor` (shown as a **Renewal**, hidden from the
prospect list by default), `past_sponsor` (a **Win-back**), `warm_intro`, `club_recorded`,
and `not_a_fit` (hidden). The file is seeded from the club's own 2026/27 commercial page
and sponsor news.

**Tagging from the page.** Every business profile has a *Club relationship* control
(current sponsor / past sponsor / warm path / not a fit, plus a note). Tags re-score the
list instantly and are saved in that browser. *Add a sponsor that isn't listed* records
sponsors the search didn't find. The **Sponsor list** button downloads the merged
`connections.csv`; drop it into the repo and re-run the pipeline to make the tags
permanent and shared. (No server needed. For shared, live tagging across several people,
the next step is a small Cloudflare Worker with a free D1/KV store.)

**What to offer, at what price.** Each prospect's suggested approach now uses the club's
real 2026/27 packages and prices (config `offers`), sized to the business: a micro firm
is pitched a £100–£200 player sponsorship, a medium one a £1,500 board or £3,000 kit slot.
Update the prices each season.

**Finding firms like the club's sponsors.** Most actual sponsors (print, signage, rail
engineering, gas, tools, tiling) were never searched for. A new
`industrial_b2b_services` sector and six new seed queries cover them. The AI step also
reads a fourth, **unscored** judgment, `local_connection` (does the website name
Dumbarton FC, a team it sponsors, or similar?). Strong evidence appears on the profile
as a **Possible lead**; once a person tags it or adds it to `connections.csv`, it counts.

Extra signals now collected on a live run: Google rating and review count (shown and in
the CSV, not scored yet), and local directors (above).

The Claude model is set by `SPONSOR_LLM_MODEL` (default `claude-haiku-5-5`).

## Employees and revenue

Read from each linked company's **filed accounts** (free, same Companies House key):

- **Employees**: the average headcount the accounts report, shown as a band
  (1–9, 10–49, 50–249, 250+). A reported 0 shows as *Directors only*.
- **Revenue**: the **reported** turnover where the accounts include it (rare for
  small firms). Otherwise an **estimate**: the ONS average turnover per business
  for the company's industry and employee size band (the reported headcount picks
  the band). Every estimate carries its working, e.g. *"6 employees puts it in the
  1–9 size band. The average UK construction business with 1–9 employees turned
  over £314,490 in 2024 (ONS Annual Business Survey size-band table) → £250k–£1m"*.
  Every firm in the same industry and size band gets the same figure, and an
  average is pulled up by larger firms, so it's shown as a band and labelled.
- **Not available** otherwise, with the reason: no company record (sole traders,
  partnerships), paper-only accounts, dormant, or no headcount. Never a guess.

```bash
python sponsor_pipeline.py --real --api --llm --accounts   # full run incl. accounts
python sponsor_pipeline.py --accounts-only --api           # add them to an existing sponsor.db
python sponsor_pipeline.py --accounts-only --no-fetch      # just recompute the bands
```

The figures come from `ons_turnover_by_size.csv`, built from the ONS Annual Business
Survey size-band workbook by `build_ons_ratios.py` (total turnover ÷ number of
businesses, same table row). It's data, not code, so it can be checked by hand.
Update it each year after ONS's May release:

```bash
pip install openpyxl
python build_ons_ratios.py abssizeband.xlsx
python sponsor_pipeline.py --accounts-only --no-fetch   # recompute bands, no API calls
```

The survey covers the non-financial economy only, so finance, public bodies and
GP, medical and dental practices show "not available" (the CSV's `sic_prefixes`
column records each partial industry's coverage). ONS dropped employment from this
table in 2026, which is why the estimate is per business, not per employee.

## Football sponsorship

Which local businesses have sponsored a football club, which club, and when. Taken from
the sponsor pages clubs publish about themselves, so it's shown as **Reported**.

```bash
python club_sponsors.py --plan            # what a live run will fetch
python club_sponsors.py --live            # all 164 clubs in clubs.yaml (cached after the first run)
python sponsor_pipeline.py --rescore      # match to businesses, apply Dumbarton's own pages, re-score
python export_sponsor_site.py
```

- **`clubs.yaml`**: the 164 clubs (SPFL, Highland, Lowland, all West of Scotland League
  divisions, 2026/27), 32 marked as near Dumbarton. Update after promotion and relegation.
- **`club_sponsors.py`**: finds each club's website (clubs.yaml, else Wikidata), honours
  robots.txt, finds sponsor/partner/commercial pages, and reads them now plus one Internet
  Archive snapshot per season back to 2021/22, including pages a redesign has removed.
  Names come from the AI model or from logos and links, and a name is kept only if it
  appears **word-for-word on the club's page**. Pages are cached. `club_coverage.csv` lists
  what was found per club and what to fix (add `website`, `sponsor_pages`, or `skip: true`).
- **`match_sponsors.py`**: matches sponsors to businesses by the website the logo links to,
  then by name (the connections.csv rule). Near-misses are kept but flagged to check.
- **Dumbarton's own pages count as the club's record**: on today's page means a Renewal
  (current sponsor), only on older pages means a Win-back (past sponsor). A person's entry
  in connections.csv still wins. Other clubs are context only and don't change the score.
- On the hub: a "Football sponsor" badge, a filter, a Football sponsorship section on each
  profile with every season and a link to the source page, and columns in the CSV.

Known gaps: clubs that only post on Facebook can't be read, archive coverage varies by
club, and a club that doesn't publish a sponsor list leaves no record.

**Sector ratings from the sponsor lists.** `sponsor_sectors.py` classifies every sponsor
found at clubs from League One down to the West of Scotland League (chain list, then what
the name says, then a near-exact Companies House match, then the AI from the name;
the method is recorded for each), counts them by sector, and compares each sector's share
of sponsors with its share of Scottish businesses (ONS business register, built into
`business_base_scotland.csv` by `build_business_base.py`). The rating is
lift / (1 + lift): 0.5 is average, 0.75 is three times as common among sponsors, and small
counts are pulled towards 0.5. Every number is in `sector_ratings.csv`. Once at least 150
sponsors are counted (config `sector_ratings`), these replace the hand-set table for every
sector SIC codes can measure; national chains and unclassified stay hand-set, and the page
labels each row Counted or Hand-set.

`python sponsor_pipeline.py --rescore` is also the way to apply an edited connections.csv
without re-running the Places search or the AI readings.

## Deploying

Deploy `site/` exactly like Sons Scouting: a Cloudflare Worker serving the folder,
behind Cloudflare Access email login. The simplest route is to drop the pages into
the Sons Scouting site so one login covers both, with a header link back:

```bash
python export_sponsor_site.py --out ../TH_DFC/site/sponsors --scouting-link ../
```

Then commit and push TH_DFC as usual. The pages contain outreach notes, so keep
whichever repo holds `site/` private. `sponsor.db` itself is never committed.

## Roadmap

- **Insight card** — a second bounded LLM call: a one-glance prep brief per
  prospect (why it fits, 2–3 conversation angles, a suggested ask range).
- **Learned Lookalike Engine** — replaces the hand-authored `sector_propensity`
  feature with a `lookalike_score` learned from who actually sponsors clubs like
  Dumbarton across Scottish football. Same slot in the composite; real data
  instead of a guessed table. This is the genuine-ML upgrade.

## Files

| File | Role |
|------|------|
| `sponsor_config.yaml` | Weights, sector table, tier map, ingest settings — all tuning |
| `sponsor_scoring.py` | Deterministic scoring core |
| `soft_features.py` | The bounded LLM layer (prompt + API + offline stub) |
| `sic_sectors.py` | SIC-code → sector mapping (makes Companies House legible) |
| `ingest_places.py` | Google Places candidate discovery (+ offline fixture) |
| `ingest_companies_house.py` | Companies House enrichment (+ offline fixture) |
| `fetch_website.py` | Homepage text fetch for the soft-feature layer |
| `data_checks.py` | Location check on Companies House matches, chain detection, sector sanity, local connections |
| `connections.csv` | Club-maintained local connections (current sponsors, known supporters) |
| `repair_published_data.py` | Applies the data checks to an already-published dataset |
| `sponsor_db.py` | SQLite store with the walled human layer |
| `sponsor_pipeline.py` | Orchestrator — builds `sponsor.db` end to end |
| `sponsor_site.html` | The website design (data is injected at export) |
| `export_sponsor_site.py` | Builds the hub page `site/index.html` (and a `lab.html` redirect) |
| `outreach.py` | Records outreach status and notes (the walled human table) |
| `ingest_accounts.py` | Employees and turnover from filed accounts (machine-readable iXBRL) |
| `revenue_estimate.py` | Employee/revenue bands; reported, else estimate with working, else not available |
| `clubs.yaml` | The clubs whose sponsor lists are collected (2026/27 tables) |
| `club_sponsors.py` | Collects club sponsor lists, now and archived back to 2021/22, with a coverage report |
| `sponsor_sectors.py` | Sector ratings counted from the club sponsor lists (with the evidence in sector_ratings.csv) |
| `match_sponsors.py` | Matches club sponsors to businesses; Dumbarton's own pages become connections |
| `build_ons_ratios.py` | Builds `ons_turnover_by_size.csv` from the ONS size-band workbook |
| `ons_turnover_by_size.csv` | Average turnover per business by industry and size (ONS, 2024 figures) |
| `sponsor_app.py` | Streamlit working view, local use |

*A decision-support prototype. It ranks and prepares; the club decides.*
