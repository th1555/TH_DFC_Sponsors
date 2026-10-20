#!/usr/bin/env python3
"""
build_ons_ratios.py  —  the reviewable table behind every revenue estimate
==========================================================================
Reads the ONS Annual Business Survey size-band workbook and writes
ons_turnover_by_size.csv: for each industry (SIC 2007 section) and employment
size band, the average turnover per business in the latest year.

    average turnover per business = total turnover / number of enterprises

Both figures come from the same ONS table row, so every estimate on the page
traces back to one line of one official spreadsheet.

Why per business, not per employee: ONS removed employment from this table
(its note 2: size-band employment isn't yet comparable with the financial
figures), so a turnover-per-employee ratio can't be built from it honestly.

Coverage (ONS note 3): the survey covers the non-financial business economy
only. Sections K (finance), O (public administration), T and U are absent, and
three sections are partial. Those limits are written into the CSV's
`sic_prefixes` column, so a business outside them shows "not available"
instead of borrowing a figure that doesn't describe it.

Get the workbook (once a year, after the May release):
  https://www.ons.gov.uk/businessindustryandtrade/business/businessservices/datasets/uknonfinancialbusinesseconomyannualbusinesssurveyemploymentsizeband/current
then:
  python build_ons_ratios.py abssizeband.xlsx           (needs: pip install openpyxl)
"""
from __future__ import annotations
import csv
import re
import sys

OUT = "ons_turnover_by_size.csv"
SHEETS = {"1 to 9": (1, 9), "10 to 49": (10, 49), "50 to 249": (50, 249), "250 and over": (250, None)}

# ONS note 3: which part of a partial section the survey covers (SIC prefixes)
PARTIAL = {
    "A": ("016", "017"),               # support activities to agriculture; hunting and trapping only
    "Q": ("861", "869", "87", "88"),   # hospitals, other human health, residential care, social work
                                       # (so GP, medical and dental practices, 86.2, are NOT covered)
    "P": (),                           # private provision only: can't tell from a SIC code, kept whole
}
PARTIAL_NOTE = {
    "A": "covers only support activities to agriculture, and hunting (SIC 01.6, 01.7)",
    "Q": "covers only SIC 86.1, 86.9, 87 and 88 (not GP, medical or dental practices)",
    "P": "private provision only",
}


def build(path, out=OUT):
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    cover = " ".join(str(c) for r in wb["Cover Sheet"].iter_rows(values_only=True) for c in r if c)
    released = (re.search(r"Release Date:\s*([0-9]{1,2} \w+ [0-9]{4})", cover) or [None, "unknown"])[1]

    rows = []
    for sheet, (lo, hi) in SHEETS.items():
        data = list(wb[sheet].iter_rows(values_only=True))
        head = next(i for i, r in enumerate(data) if r and r[0] == "SIC")
        body = [r for r in data[head + 1:] if r and r[0] and isinstance(r[3], int)]
        year = max(r[3] for r in body)
        for r in body:
            if r[3] != year or r[0] == "A to S (part)":
                continue
            letter = str(r[0]).strip()[0]
            n, turnover = r[4], r[5]
            if not isinstance(n, (int, float)) or not isinstance(turnover, (int, float)) or n <= 0:
                continue                                  # [c], [low], [x]: suppressed, skip
            rows.append(dict(
                section=letter, section_name=str(r[1]).strip(), size_band=sheet,
                emp_min=lo, emp_max="" if hi is None else hi, year=year,
                enterprises=int(n), turnover_gbp_m=turnover,
                avg_turnover_per_business_gbp=round(turnover * 1e6 / n),
                sic_prefixes=" ".join(PARTIAL.get(letter, ())),
                coverage_note=PARTIAL_NOTE.get(letter, ""),
                source=f"ONS Annual Business Survey size-band table, released {released}"))
    rows.sort(key=lambda r: (r["section"], r["emp_min"]))
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out}: {len(rows)} rows, {len({r['section'] for r in rows})} industries, "
          f"year {rows[0]['year']}, {rows[0]['source']}")
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else OUT)
