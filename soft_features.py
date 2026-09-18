#!/usr/bin/env python3
"""
Soft-feature layer  —  Dumbarton FC Sponsor Intelligence Tool (the LLM component)
=================================================================================
The ONE genuinely LLM-powered part of the sponsor tool — and deliberately the
smallest. It reads a business's website text + its Companies House facts and
returns exactly THREE soft judgments, each as {value, confidence, justification}:

    community_affinity   how locally rooted / community-proud the business is
    brand_fit            how well sponsoring a community club suits its positioning
    csr_signal           visible community-mindedness EVIDENCED in the context

What it does NOT do: it never decides the fit score. A separate deterministic
engine (sponsor_scoring.py) does that. This layer only supplies three of its
inputs. That separation is the whole anti-"ask-the-LLM-for-87/100" design.

Three guardrails, in order of importance:
  1. ANTI-HALLUCINATION (in the prompt): the model may use ONLY the facts in the
     provided context; every justification must cite one; it must not invent
     community ties the text doesn't state.
  2. CONFIDENCE (here + scoring): a thin/empty website yields low confidence;
     sponsor_scoring.soft() then pulls low-confidence reads toward neutral so
     they can't swing the score. Single source of truth for that pull.
  3. SCHEMA VALIDATION (here): strict-JSON parse, range-clamp, fail-safe to
     neutral+zero-confidence on any malformed output — a bad LLM response can
     never poison a score, only decline to move it.

Two modes, same output shape:
  * mode="stub"  (default) — a deterministic offline mock that keyword-reads the
    text. No API key, runs anywhere, lets you exercise the whole pipeline.
  * mode="llm"   — the real Anthropic call. Needs ANTHROPIC_API_KEY in the env.
    Batch these to control cost; Haiku is the right cheap model for the job.

Run:  python soft_features.py            # offline stub demo + handoff to scoring
      ANTHROPIC_API_KEY=… python soft_features.py --llm   # real call
Deps: requests (only for --llm), pandas   (+ sponsor_scoring.py beside it)
"""
from __future__ import annotations
import os
import re
import sys
import json
import pandas as pd

DEFAULT_MODEL = "claude-haiku-4-5"     # cheap + fast; this is a high-volume job
MAX_TOKENS = 500
FEATURES = ("community_affinity", "brand_fit", "csr_signal")


# ===========================================================================
# THE PROMPT  —  the real deliverable of this layer
# ===========================================================================
SYSTEM_PROMPT = (
    "You are a careful research assistant scoring a local business as a potential "
    "sponsor for Dumbarton FC, a small Scottish League Two club that sells LOCAL "
    "AFFINITY, not reach. You output ONLY three soft judgments, as strict JSON. "
    "You NEVER decide the final score — a separate deterministic system does that. "
    "You may use ONLY the facts in the PROVIDED CONTEXT. Do not use outside "
    "knowledge or assumptions about the business. If the context is thin or "
    "absent, say so and return LOW confidence. Every justification must reference "
    "a specific fact or phrase from the context. Never invent community ties, "
    "sponsorships, or local involvement that the context does not state."
)

USER_TEMPLATE = """PROVIDED CONTEXT (the only facts you may use):
- Business name: {name}
- Sector (Companies House SIC): {sector}
- Registered ~{age} years ago; accounts status: {accounts_status}
- Distance from the stadium: {distance_km} km
- Website text (verbatim, may be empty):
\"\"\"
{website_text}
\"\"\"

Score these three features, each 0.0-1.0, with a confidence (0.0-1.0) reflecting
how well the CONTEXT supports the judgment, and a one-line justification that
cites a specific fact or phrase from the context:

1. community_affinity — how locally rooted / community-proud this business is
   (local identity, town ties, family/independent ownership, long local history).
2. brand_fit — how well sponsoring a community football club suits this
   business's positioning (local-facing, values visibility in the town, audience
   overlap). Online-only / national brands fit poorly.
3. csr_signal — visible community-mindedness (charity, grassroots/youth support,
   local causes, existing sponsorships) EVIDENCED IN THE CONTEXT.

Rules:
- Use ONLY the context above. If it does not support a judgment, return LOW
  confidence and say so in the justification.
- Do NOT reward a business for community ties the context does not mention.
- Return STRICT JSON ONLY — no preamble, no markdown fences — exactly this shape:
{{
  "community_affinity": {{"value": 0.0, "confidence": 0.0, "justification": ""}},
  "brand_fit":          {{"value": 0.0, "confidence": 0.0, "justification": ""}},
  "csr_signal":         {{"value": 0.0, "confidence": 0.0, "justification": ""}}
}}"""


def build_user_prompt(rec: dict) -> str:
    return USER_TEMPLATE.format(
        name=rec.get("name", "?"),
        sector=rec.get("sector", "unknown"),
        age=rec.get("company_age", "?"),
        accounts_status="dormant" if rec.get("accounts_dormant") else "active",
        distance_km=rec.get("distance_km", "?"),
        website_text=(rec.get("website_text") or "").strip() or "(no website text found)",
    )


# ===========================================================================
# SCHEMA VALIDATION  —  a malformed LLM reply can only decline to move a score
# ===========================================================================
def _clamp01(x, default):
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return default


def validate(raw: dict) -> dict:
    """Coerce the model's JSON into a safe, in-range triple. Any missing/invalid
    field fails SAFE: value=neutral(0.5), confidence=0.0 -> scoring pulls it fully
    to neutral, so it neither helps nor hurts."""
    out = {}
    for f in FEATURES:
        blk = raw.get(f) if isinstance(raw, dict) else None
        if not isinstance(blk, dict):
            out[f] = {"value": 0.5, "confidence": 0.0,
                      "justification": "missing field — defaulted to neutral"}
            continue
        out[f] = {
            "value": _clamp01(blk.get("value"), 0.5),
            "confidence": _clamp01(blk.get("confidence"), 0.0),
            "justification": str(blk.get("justification") or "").strip()[:200]
                             or "no justification given",
        }
    return out


def parse_json_reply(text: str) -> dict:
    """Strip any stray markdown fences and parse. On failure -> all-neutral."""
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return validate(json.loads(t))
    except (json.JSONDecodeError, TypeError):
        return validate({})     # fail-safe: neutral, zero confidence


# ===========================================================================
# MODE 1 — the real Anthropic call  (needs ANTHROPIC_API_KEY)
# ===========================================================================
def derive_llm(rec: dict, model=DEFAULT_MODEL, api_key=None) -> dict:
    import requests
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("no ANTHROPIC_API_KEY set — run in stub mode to go offline")
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": MAX_TOKENS,
              "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": build_user_prompt(rec)}]},
        timeout=40,
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json().get("content", [])
                   if b.get("type") == "text")
    return parse_json_reply(text)


# ===========================================================================
# MODE 2 — deterministic offline STUB  (keyword-reads the text; no API)
# ===========================================================================
# Not the product — a stand-in so the pipeline runs end-to-end with no key. It
# mimics the SHAPE of the real output (values + honest confidence from how much
# the text actually says), so you can watch it flow into the score.
_LOCAL = ("local", "community", "town", "family", "family-run", "independent",
          "proud", "roots", "generations", "established", "since 19", "since 20",
          "vale of leven", "dumbarton", "clydebank", "west dunbartonshire")
_CSR = ("charity", "fundrais", "sponsor", "support", "donat", "grassroots",
        "youth", "school", "foodbank", "volunteer", "community club", "boys club",
        "girls club", "raised")
_ANTI = ("nationwide", "online only", "online-only", "e-commerce", "ecommerce",
         "ship anywhere", "uk-wide", "delivered to your door", "worldwide")


def _hits(text, words):
    t = text.lower()
    return sum(1 for w in words if w in t)


def derive_stub(rec: dict) -> dict:
    text = (rec.get("website_text") or "").strip()
    words = len(text.split())
    if words == 0:                        # nothing to read -> low confidence
        return validate({})               # neutral 0.5, conf 0.0

    local_h, csr_h, anti_h = _hits(text, _LOCAL), _hits(text, _CSR), _hits(text, _ANTI)
    # confidence rises with how much text there is to judge (saturating)
    base_conf = min(0.95, 0.35 + words / 60.0)

    comm = min(1.0, 0.15 + 0.18 * local_h - 0.15 * anti_h)
    comm = max(0.0, comm)
    csr = min(1.0, 0.10 + 0.20 * csr_h)
    brand = min(1.0, 0.20 + 0.14 * local_h + 0.10 * csr_h - 0.25 * anti_h)
    brand = max(0.0, brand)

    def just(kind, h):
        if h == 0 and kind != "brand":
            return f"no {kind} signal in the provided text"
        return f"{h} {kind} phrase(s) found in website text"

    return validate({
        "community_affinity": {"value": comm, "confidence": base_conf,
                               "justification": just("local", local_h)},
        "brand_fit": {"value": brand, "confidence": base_conf * 0.95,
                      "justification": (f"{anti_h} online/national phrase(s) counter fit"
                                        if anti_h else just("local-facing", local_h))},
        "csr_signal": {"value": csr, "confidence": base_conf * 0.9,
                       "justification": just("CSR", csr_h)},
    })


# ===========================================================================
# ENRICH — attach the six flat fields sponsor_scoring.score_business() expects
# ===========================================================================
def enrich(rec: dict, mode="stub", **kw) -> dict:
    derived = derive_stub(rec) if mode == "stub" else derive_llm(rec, **kw)
    flat = dict(rec)
    flat.update({
        "community_affinity": derived["community_affinity"]["value"],
        "community_conf":     derived["community_affinity"]["confidence"],
        "brand_fit":          derived["brand_fit"]["value"],
        "brand_conf":         derived["brand_fit"]["confidence"],
        "csr_signal":         derived["csr_signal"]["value"],
        "csr_conf":           derived["csr_signal"]["confidence"],
        "_soft_justif":       {f: derived[f]["justification"] for f in FEATURES},
    })
    return flat


# ===========================================================================
# DEMO — a few businesses with realistic website copy, read offline
# ===========================================================================
DEMO = [
    dict(name="Marbill Coaches", sector="transport_coaches_haulage", company_age=38,
         accounts_dormant=False, distance_km=1.2, size_band="small",
         warm_intro="Board — known sponsor",
         website_text=(
             "Marbill Coaches is a family-run coach operator based in the Vale of "
             "Leven, proudly serving West Dunbartonshire since 1985. We support "
             "local grassroots sport and community groups across Dumbarton and "
             "have sponsored youth teams in the area for over a decade.")),
    dict(name="Bonhill Funeralcare", sector="funeral_directors", company_age=40,
         accounts_dormant=False, distance_km=2.6, size_band="micro", warm_intro=None,
         website_text=(
             "An independent, family-owned funeral director rooted in the local "
             "community for three generations. We are proud of our ties to the "
             "town and regularly support local charity fundraising.")),
    dict(name="ByteBargains Online", sector="online_ecommerce", company_age=4,
         accounts_dormant=False, distance_km=7.0, size_band="medium", warm_intro=None,
         website_text=(
             "ByteBargains ships electronics nationwide, delivered to your door. "
             "UK-wide next-day delivery on thousands of online-only deals.")),
    dict(name="Riverside Physio", sector="health_fitness_physio_dental", company_age=6,
         accounts_dormant=False, distance_km=4.2, size_band="micro", warm_intro=None,
         website_text=""),      # no website found -> should read as LOW confidence
]


if __name__ == "__main__":
    mode = "llm" if "--llm" in sys.argv else "stub"
    print("=" * 74)
    print(f"SOFT-FEATURE LAYER  —  deriving 3 soft features  (mode: {mode})")
    print("=" * 74)
    if mode == "llm" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("no ANTHROPIC_API_KEY in env — falling back to offline stub.\n")
        mode = "stub"

    try:
        import sponsor_scoring as ss
        cfg = ss.load_config()
    except Exception as e:
        cfg = None
        print(f"(sponsor_scoring not scoring this run: {e})\n")

    for rec in DEMO:
        flat = enrich(rec, mode=mode)
        print(f"\n■ {rec['name']}  ({rec['sector']})")
        wt = (rec["website_text"] or "").strip()
        print(f"  website text: {'(none found)' if not wt else wt[:90] + '…'}")
        # print the three derived judgments with their confidence + justification
        for f, valk, confk in (("community_affinity", "community_affinity", "community_conf"),
                               ("brand_fit", "brand_fit", "brand_conf"),
                               ("csr_signal", "csr_signal", "csr_conf")):
            print(f"    {f:<19} value={flat[valk]:.2f}  conf={flat[confk]:.2f}"
                  f"   \"{flat['_soft_justif'][f]}\"")
        if cfg is not None:
            r = ss.score_business(flat, cfg)
            print(f"  -> deterministic fit score: {r['fit_score']}  "
                  f"(community pts={r['contrib']['community_affinity']:.1f}, "
                  f"brand pts={r['contrib']['brand_fit']:.1f}, "
                  f"csr pts={r['contrib']['csr_signal']:.1f})")

    print("\n" + "-" * 74)
    print("Note how Riverside Physio (no website text) comes back at confidence 0.00,")
    print("so scoring pulls its soft features to neutral 0.5 — it can't be rewarded")
    print("for community ties nothing in the context supports. ByteBargains reads")
    print("confidently NON-local from its own copy. That is the guardrail working.")
