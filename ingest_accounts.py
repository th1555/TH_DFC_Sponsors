#!/usr/bin/env python3
"""
ingest_accounts.py  —  employees and turnover from filed accounts
==================================================================
For a company we've already matched on Companies House, read its most recent
accounts and pull two recorded figures:

  * average number of employees during the year (small and micro companies
    have had to report this since the 2015 regulations)
  * turnover, where the company filed it (rare at this size: small and micro
    companies can file without a profit and loss account)

Route (free, same COMPANIES_HOUSE_API_KEY as the rest of the pipeline):

  /company/{n}/filing-history?category=accounts   newest accounts filings
      -> links.document_metadata                  one document per filing
  document API /document/{id}                     which formats exist
      -> "application/xhtml+xml" in resources     = machine-readable (iXBRL)
  document API /document/{id}/content             with that Accept header

Accounts filed on paper (or as a PDF only) have no machine-readable version, so
they come back "not available". We never guess a figure.

The iXBRL reading follows the field names and quirks handled by the UK
government's open-source parser (stream-read-xbrl, Department for Business and
Trade, MIT licence): dash placeholders for "none", prefixes like "2017 - 2",
stray minus signs on headcounts, and comma/space number formats.

Two modes, identical output:
  mode="fixture" (default)  canned real-shaped filings; offline
  mode="api"                the live calls

python ingest_accounts.py          runs the offline checks
"""
from __future__ import annotations
import re
import time
from datetime import date
from decimal import Decimal, InvalidOperation

import ingest_companies_house as ch

DOC_BASE = "https://document-api.company-information.service.gov.uk"
FILINGS_URL = "https://find-and-update.company-information.service.gov.uk/company/{n}/filing-history"
XHTML = "application/xhtml+xml"

LOOKBACK = 2           # if the newest filing is paper-only, try one older filing
MAX_AGE_YEARS = 3      # older than this and the figure is too stale to show

EMPLOYEE_TAGS = ("AverageNumberEmployeesDuringPeriod", "EmployeesTotal")
TURNOVER_TAGS = ("TurnoverRevenue", "TurnoverGrossOperatingRevenue")

# accounts status -> what the page says when there's no figure
STATUS_TEXT = {
    "reported": None,
    "no_company": "No Companies House record linked",
    "dormant": "Dormant company accounts",
    "no_accounts": "No accounts filed yet",
    "paper_only": "Accounts filed on paper or as a scan, not machine-readable",
    "stale": "Latest readable accounts are over 3 years old",
    "not_in_accounts": "Not stated in the filed accounts",
    "error": "Couldn't read the accounts this run",
}


# ---------------------------------------------------------------------------
# PARSE  an iXBRL document -> recorded figures.  Pure, fully testable.
# ---------------------------------------------------------------------------
_NULL = {"", "-", "–", "—", "nil", "none", "n/a"}


def _number(el, text: str):
    """One ix:nonFraction -> Decimal, honouring format, scale and sign."""
    if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
        return None
    t = re.sub(r"\s+", " ", text or "").strip()
    fmt = (el.get("format") or "").rpartition(":")[2].lower()
    if fmt in ("zerodash", "fixedzero", "numwordsen") and t.lower() in _NULL | {"zero", "no"}:
        return Decimal(0)
    if t.lower() in _NULL:
        return None
    t = re.sub(r"(.*:)|(.+- )", "", t)                # "2017 - 2" -> "2"
    t = t.strip().strip("()")                          # (1,234) -> 1,234 (sign is an attribute)
    if fmt in ("numdotcomma", "numcommadecimal", "num-comma-decimal"):
        t = t.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        t = t.replace(",", "").replace(" ", "")
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    if el.get("sign") == "-":
        v = -v
    return v * (Decimal(10) ** int(el.get("scale") or 0))


def _contexts(root):
    """context id -> (end date, has a dimension segment)."""
    out = {}
    for ctx in root.iter():
        if not isinstance(ctx.tag, str) or not ctx.tag.endswith("}context"):
            continue
        end = None
        for e in ctx.iter():
            if isinstance(e.tag, str) and e.tag.rpartition("}")[2] in ("endDate", "instant") and e.text:
                end = e.text.strip()[:10]
        segmented = any(isinstance(e.tag, str) and e.tag.rpartition("}")[2] in ("segment", "scenario")
                        for e in ctx.iter())
        out[ctx.get("id")] = (end, segmented)
    return out


def parse_ixbrl(content) -> dict:
    """Return {employees, turnover, period_end}. Values come from the current
    period: the latest end date, ignoring breakdowns (dimensioned contexts)."""
    from lxml import etree
    if isinstance(content, str):
        content = content.encode("utf-8")
    root = etree.fromstring(content, etree.XMLParser(recover=True, huge_tree=True))
    if root is None:
        return {"employees": None, "turnover": None, "period_end": None}
    ctx = _contexts(root)

    def best(tags):
        found = []
        for el in root.iter():
            if not isinstance(el.tag, str) or el.tag.rpartition("}")[2] != "nonFraction":
                continue
            if (el.get("name") or "").rpartition(":")[2] not in tags:
                continue
            end, segmented = ctx.get(el.get("contextRef"), (None, True))
            if segmented or not end:
                continue
            text = "".join(t for t in el.itertext())
            v = _number(el, text)
            if v is not None:
                found.append((end, tags.index((el.get("name") or "").rpartition(":")[2]), v))
        if not found:
            return None, None
        latest = max(f[0] for f in found)
        pick = sorted(f for f in found if f[0] == latest)[0]   # preferred tag first
        return pick[2], latest

    emp, emp_end = best(EMPLOYEE_TAGS)
    turn, turn_end = best(TURNOVER_TAGS)
    return {
        "employees": int(abs(emp).to_integral_value()) if emp is not None else None,
        "turnover": int(turn.to_integral_value()) if turn is not None else None,
        "period_end": emp_end or turn_end,
    }


# ---------------------------------------------------------------------------
# REAL API  (shares the Companies House pacing: ~600 calls per 5 minutes)
# ---------------------------------------------------------------------------
def _doc_get(path, api_key=None, accept="application/json", tries=5):
    import requests
    for attempt in range(tries):
        wait = ch.MIN_INTERVAL_S - (time.monotonic() - ch._last_call[0])
        if wait > 0:
            time.sleep(wait)
        ch._last_call[0] = time.monotonic()
        # the content call redirects to file storage; requests drops the key
        # when the host changes, which is what that storage expects
        r = requests.get(f"{DOC_BASE}{path}", auth=ch._auth(api_key),
                         headers={"Accept": accept}, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            pause = int(r.headers.get("Retry-After") or 0) or min(60, 5 * 2 ** attempt)
            print(f"  [accounts] {r.status_code}, waiting {pause}s and retrying")
            time.sleep(pause)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json() if accept == "application/json" else r.content
    return None


def filings_api(company_number, api_key=None):
    return ch._get(f"/company/{company_number}/filing-history", api_key,
                   {"category": "accounts", "items_per_page": 10}).get("items", [])


def document_meta_api(doc_id, api_key=None):
    return _doc_get(f"/document/{doc_id}", api_key) or {}


def document_content_api(doc_id, api_key=None):
    return _doc_get(f"/document/{doc_id}/content", api_key, accept=XHTML)


# ---------------------------------------------------------------------------
# OFFLINE FIXTURE  (real-shaped filings for the fixture companies)
# ---------------------------------------------------------------------------
def _ixbrl(end, start, employees=None, turnover=None, prior_employees=None, extra=""):
    """A minimal iXBRL document in the shape Companies House serves."""
    facts = []
    if employees is not None:
        facts.append(f'<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" '
                     f'contextRef="cur" unitRef="pure" decimals="0">{employees}</ix:nonFraction>')
    if prior_employees is not None:
        facts.append(f'<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" '
                     f'contextRef="prev" unitRef="pure" decimals="0">{prior_employees}</ix:nonFraction>')
    if turnover is not None:
        facts.append(f'<ix:nonFraction name="core:TurnoverRevenue" contextRef="cur" unitRef="GBP" '
                     f'decimals="0" format="ixt:numdotdecimal">{turnover}</ix:nonFraction>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:core="http://xbrl.frc.org.uk/fr/2023-01-01/core"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12">
<body><div style="display:none"><ix:header><ix:resources>
<xbrli:context id="cur"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">X</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="prev"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">X</xbrli:identifier></xbrli:entity>
<xbrli:period><xbrli:startDate>2023-04-01</xbrli:startDate><xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
</ix:resources></ix:header></div>
<p>Average number of employees: {' '.join(facts)}</p>{extra}</body></html>"""


_FIX_FILINGS = {
    # small company, employees and turnover both filed
    "SC000001": [dict(date="2025-12-10", description="accounts-with-accounts-type-small",
                      description_values={"made_up_date": "2025-03-31"},
                      links={"document_metadata": "https://frontend-doc-api.company-information.service.gov.uk/document/FIX1"})],
    # micro company: employees only (the usual case)
    "SC000003": [dict(date="2025-09-02", description="accounts-with-accounts-type-micro-entity",
                      description_values={"made_up_date": "2025-02-28"},
                      links={"document_metadata": ".../document/FIX3"})],
    # newest filing on paper, the one before is readable
    "SC000004": [dict(date="2025-11-20", description="accounts-with-accounts-type-micro-entity",
                      description_values={"made_up_date": "2025-06-30"},
                      links={"document_metadata": ".../document/FIX4PAPER"}),
                 dict(date="2024-11-02", description="accounts-with-accounts-type-micro-entity",
                      description_values={"made_up_date": "2024-06-30"},
                      links={"document_metadata": ".../document/FIX4"})],
    # director-only company: 0 employees
    "SC000005": [dict(date="2025-08-15", description="accounts-with-accounts-type-total-exemption-full",
                      description_values={"made_up_date": "2025-03-31"},
                      links={"document_metadata": ".../document/FIX5"})],
    "SC000007": [dict(date="2025-06-01", description="accounts-with-accounts-type-dormant",
                      description_values={"made_up_date": "2025-03-31"},
                      links={"document_metadata": ".../document/FIX7"})],
}
_FIX_DOCS = {
    "FIX1": _ixbrl("2025-03-31", "2024-04-01", employees=23, turnover="2,450,000", prior_employees=21),
    "FIX3": _ixbrl("2025-02-28", "2024-03-01", employees=3),
    "FIX4": _ixbrl("2024-06-30", "2023-07-01", employees="2023 - 6"),
    "FIX5": _ixbrl("2025-03-31", "2024-04-01", employees="0"),   # directors only
}


def filings_fixture(company_number, **_):
    return _FIX_FILINGS.get(company_number, [])


def document_meta_fixture(doc_id, **_):
    if doc_id in _FIX_DOCS:
        return {"resources": {"application/pdf": {}, XHTML: {}}}
    return {"resources": {"application/pdf": {}}}


def document_content_fixture(doc_id, **_):
    return _FIX_DOCS.get(doc_id)


# ---------------------------------------------------------------------------
# READ — company number -> recorded figures + status
# ---------------------------------------------------------------------------
def _empty(company_number, status, **kw):
    out = dict(company_number=company_number, status=status, employees=None,
               turnover=None, period_end=None, filed_on=None, accounts_type=None,
               source_url=FILINGS_URL.format(n=company_number) if company_number else None)
    out.update(kw)
    return out


def read_accounts(company_number, mode="fixture", api_key=None, today=None) -> dict:
    if not company_number:
        return _empty(None, "no_company")
    get_filings = filings_fixture if mode == "fixture" else filings_api
    get_meta = document_meta_fixture if mode == "fixture" else document_meta_api
    get_content = document_content_fixture if mode == "fixture" else document_content_api
    today = today or date.today()
    try:
        items = [i for i in get_filings(company_number, api_key=api_key)
                 if str(i.get("type") or "AA").upper().startswith("AA")]
        if not items:
            return _empty(company_number, "no_accounts")
        items.sort(key=lambda i: str(i.get("date") or ""), reverse=True)
        if "dormant" in str(items[0].get("description") or ""):
            return _empty(company_number, "dormant", filed_on=items[0].get("date"))
        saw_old = False
        for item in items[:LOOKBACK]:
            made_up = (item.get("description_values") or {}).get("made_up_date")
            if made_up and (today - date.fromisoformat(made_up[:10])).days > MAX_AGE_YEARS * 365.25:
                saw_old = True
                continue
            link = (item.get("links") or {}).get("document_metadata")
            if not link:
                continue
            doc_id = link.rstrip("/").rpartition("/")[2]
            if XHTML not in (get_meta(doc_id, api_key=api_key) or {}).get("resources", {}):
                continue
            content = get_content(doc_id, api_key=api_key)
            if not content:
                continue
            fig = parse_ixbrl(content)
            acct = str(item.get("description") or "").replace("accounts-with-accounts-type-", "")
            status = "reported" if (fig["employees"] is not None or fig["turnover"] is not None) \
                else "not_in_accounts"
            return dict(company_number=company_number, status=status,
                        employees=fig["employees"], turnover=fig["turnover"],
                        period_end=fig["period_end"] or made_up, filed_on=item.get("date"),
                        accounts_type=acct or None,
                        source_url=FILINGS_URL.format(n=company_number))
        return _empty(company_number, "stale" if saw_old else "paper_only")
    except Exception as e:                      # one bad filing never stops a run
        print(f"  [accounts] {company_number}: {e}")
        return _empty(company_number, "error")


def read_many(company_numbers, mode="fixture", api_key=None, today=None) -> list[dict]:
    nums = sorted({str(n) for n in company_numbers if n})
    out = []
    for i, n in enumerate(nums, 1):
        out.append(read_accounts(n, mode=mode, api_key=api_key, today=today))
        if mode != "fixture" and (i % 25 == 0 or i == len(nums)):
            print(f"  accounts: {i}/{len(nums)}")
    return out


# ---------------------------------------------------------------------------
# offline checks
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    TODAY = date(2026, 10, 19)
    print("Accounts reader — offline fixture checks\n" + "=" * 46)
    want = {
        "SC000001": ("reported", 23, 2450000, "2025-03-31"),   # current year, not prior (21)
        "SC000003": ("reported", 3, None, "2025-02-28"),
        "SC000004": ("reported", 6, None, "2024-06-30"),       # paper newest -> older readable
        "SC000005": ("reported", 0, None, "2025-03-31"),        # directors only
        "SC000007": ("dormant", None, None, None),
        "SC000002": ("no_accounts", None, None, None),
        None: ("no_company", None, None, None),
    }
    for n, (st, emp, turn, end) in want.items():
        r = read_accounts(n, today=TODAY)
        got = (r["status"], r["employees"], r["turnover"], r["period_end"])
        assert got == (st, emp, turn, end), (n, got)
        print(f"  {str(n):9} -> {st:16} employees={emp}  turnover={turn}  period_end={end}")

    # number quirks seen in real filings
    from lxml import etree
    def num(text, **attrs):
        el = etree.Element("x", **attrs)
        return _number(el, text)
    assert num("1,234") == 1234
    assert num("1.234", format="ixt:numdotcomma") == 1234
    assert num("12", scale="3") == 12000
    assert num("5", sign="-") == -5
    assert num("–") is None and num("") is None
    assert num("-", format="ixt:zerodash") == 0
    assert num("2017 - 2") == 2
    print("  number formats: thousands, comma-decimal, scale, sign, dashes, prefixes ✓")

    # a breakdown (dimensioned) context must not be read as the company total,
    # and a figure split across nested tags must still read whole
    doc = _ixbrl("2025-03-31", "2024-04-01", turnover="900,000").replace(
        "</ix:resources>",
        '<xbrli:context id="seg"><xbrli:entity><xbrli:identifier scheme="x">X</xbrli:identifier>'
        '<xbrli:segment><xbrldi:explicitMember dimension="core:SegmentsDimension">core:Seg1'
        '</xbrldi:explicitMember></xbrli:segment></xbrli:entity><xbrli:period>'
        '<xbrli:startDate>2024-04-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate>'
        '</xbrli:period></xbrli:context></ix:resources>').replace(
        "</p>", '<ix:nonFraction name="core:TurnoverRevenue" contextRef="seg" unitRef="GBP">5,000</ix:nonFraction>'
        '<ix:nonFraction name="core:AverageNumberEmployeesDuringPeriod" contextRef="cur" unitRef="pure">'
        '<span>1</span><span>4</span></ix:nonFraction></p>')
    f = parse_ixbrl(doc)
    assert f == {"employees": 14, "turnover": 900000, "period_end": "2025-03-31"}, f
    print("  breakdown contexts ignored, nested tags read whole ✓")
    print("\nall accounts checks passed")
