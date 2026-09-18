# Dumbarton FC — Sponsor Intelligence Tool

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
  sponsor_app.py       Streamlit action queue — ranked, auditable, status-editable
```

`sponsor_pipeline.py` runs the whole chain and builds `sponsor.db`.

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

python sponsor_pipeline.py        # build sponsor.db (offline, synthetic data)
streamlit run sponsor_app.py      # open the action queue
```

Real LLM soft-feature derivation (optional):

```bash
export ANTHROPIC_API_KEY=…        # never commit this
python sponsor_pipeline.py --llm
```

## Going from demo to real data

The pipeline ships on **synthetic businesses with realistic website copy** so the
whole thing runs offline. Two clean seams swap in real data with no change to the
scoring or storage logic:

- **Google Places** → business name, sector, location (distance to the ground).
- **Companies House (free API)** → company age, size band, accounts status.
- Fetch each candidate's homepage text → feed `soft_features.py` in `--llm` mode.

`sponsor_scoring.score_business()` scores a real enriched record identically to a
synthetic one — that's the seam.

## Deploying

The app is fine on **Streamlit Cloud**: if `sponsor.db` is absent it builds a
clearly-labelled demo store, so the public demo works with no database in the
repo. Because Cloud's filesystem is ephemeral, status edits there won't persist
across restarts — point `DUMBARTON_SPONSOR_DB` at a hosted DB (e.g. Supabase /
Postgres) when durable, multi-user editing matters.

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
| `sponsor_config.yaml` | Weights, sector table, tier map — all tuning, no code changes |
| `sponsor_scoring.py` | Deterministic scoring core |
| `soft_features.py` | The bounded LLM layer (prompt + API + offline stub) |
| `sponsor_db.py` | SQLite store with the walled human layer |
| `sponsor_pipeline.py` | Orchestrator — builds `sponsor.db` end to end |
| `sponsor_app.py` | Streamlit action-queue UI |

*A decision-support prototype. It ranks and prepares; the club decides.*
