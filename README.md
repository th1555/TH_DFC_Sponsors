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
  export_sponsor_site.py   the club website: one static page in the Sons
                           Scouting design, plus a lab page for the prototype parts
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

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

python sponsor_pipeline.py        # build sponsor.db (offline, demo businesses)
python export_sponsor_site.py     # write site/index.html and site/lab.html
```

Open `site/index.html` in a browser. Demo data is labelled as such on the page.

## The club website

Same design, structure and conventions as Sons Scouting, so the two read as one
product: the ranked list with filters, a table and a "map of fit" chart, CSV
download, and a profile for each business showing where its points come from.

It follows the same **shipped / lab split**:

- **`site/index.html` (shipped)** shows recorded facts only (distance, sector,
  company record) and the **Local fit** built from them: distance, sector,
  trading record and size, scaled to 100.
- **`site/lab.html` (prototype)** adds the website readings (local roots, fit
  with a town club, community support) and the **Fit score** that includes them.
  Same login, not linked from the main page. The readings are judgments, and on
  the keyword proxy they are rough, so they stay off the page the club relies on.

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
| `sponsor_db.py` | SQLite store with the walled human layer |
| `sponsor_pipeline.py` | Orchestrator — builds `sponsor.db` end to end |
| `sponsor_site.html` | The website design (data is injected at export) |
| `export_sponsor_site.py` | Builds `site/index.html` (shipped) and `site/lab.html` (prototype) |
| `outreach.py` | Records outreach status and notes (the walled human table) |
| `sponsor_app.py` | Streamlit working view, local use |

*A decision-support prototype. It ranks and prepares; the club decides.*
