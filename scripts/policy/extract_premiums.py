"""Read premiums, the age curve and the Los Angeles rating regions out of their official PDFs, into CSVs. No LLM.

    python scripts/policy/extract_premiums.py [--pdfs data/policy_pdfs/premiums] [--out data/plans]

Every number is parsed from the PDF text layer (pdftotext -layout) and kept with its page and the verbatim line it
came from; nobody types a premium. Which booklet row prices which plan (PLAN_ROWS) is development-time knowledge.

Input (download with the URLs in SOURCES into --pdfs; SHA-256 checked):
  Covered California "2026 Regional Rates by County" (as of 2025-08-01, preliminary): monthly premium per plan line
      and metal tier, for a single 25- and 40-year-old, per rating region
  UCLA "2026-27 UC SHIP Coverage & Waiver Period Calendar": the premium per quarter
  CMS "Final Guidance Regarding Age Curves" (2016-12-16), Appendix I: the federal default age curve. California
      has no curve of its own, so this one applies
  Covered California "Plan Type by Rating Region - Zip Code" (2019 QHP application, Attachment A): the rating region
      of every Los Angeles County ZIP code. The regions are fixed by statute (Health and Safety Code 1357.512)
Output, under --out: premiums.csv, age_curve.csv, rating_regions.csv (read by scripts/policy/build_seed.py).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CCA_BOOKLET = "https://hbex.coveredca.com/toolkit/downloads/CCA_26_QHP_Plan_Rates_by_County.pdf"
UCLA_CALENDAR = "https://www.studenthealth.ucla.edu/file/7b193d21-e33c-4d25-adf7-e9ddf83dbf9c"
CMS_AGE_CURVE = ("https://www.cms.gov/CCIIO/Resources/Regulations-and-Guidance/Downloads/"
                 "Final-Guidance-Regarding-Age-Curves-and-State-Reporting-12-16-16.pdf")
CCA_ZIPS = ("https://hbex.coveredca.com/stakeholders/plan-management/PDFs/March-1-2018_2019-Qualified-Health-Plan-"
            "Application-Individual-Marketplace/Attachment%20A%20-%20Plan%20Type%20by%20Rating%20Region%20-%20Zip%20"
            "Code%20Individual%20QHP%20-%20Updated.pdf")
# file name -> (url, sha256 of the file read on 2026-10-24)
SOURCES = {
    "CCA_26_QHP_Plan_Rates_by_County.pdf": (CCA_BOOKLET,
                                            "2cad2add89eb3b4b154698d1947bda3a24992e2ecfc158274f3369290b423c93"),
    "ucla_ucship_2026_27_calendar.pdf": (UCLA_CALENDAR,
                                         "36ded323b02b33fb4e79e21844b53794a33f6863d2620ecaa8d053ad78c81932"),
    "cms_age_curve_guidance_2016-12-16.pdf": (CMS_AGE_CURVE,
                                              "5563e6d8fd19fcd397037c5c8e83c9a891b2cada7eb3e1473c21da60eacfc060"),
    "cca_rating_region_zip.pdf": (CCA_ZIPS, "1d5a6edf6656060b4f9a194fa16482185d69af199c588824565f330af4842f8b"),
}
METALS = ["Minimum", "Bronze HDHP", "Bronze", "Silver", "Gold", "Platinum"]  # the booklet's column order
# plan_id -> (booklet row label without its "*", metal column). Covered California standard plans are sold as one
# product line per insurer and network type; the metal picks the column. Health Net's booklet line is "Health Net
# HMO" (the SBCs call the product Ambetter HMO). A plan the booklet has no price for in Los Angeles gets no rows.
# Anthem Silver 70 EPO and Health Net Bronze 60 HMO were removed on 2026-10-24: the booklet's Los Angeles pages have
# no Anthem EPO line and no Health Net HMO Bronze price, so they are not sold there.
PLAN_ROWS = {
    "anthem_gold80_hmo_2026": ("Anthem HMO", "Gold"),
    "healthnet_silver70_hmo_2026": ("Health Net HMO", "Silver"),
    "healthnet_gold80_hmo_2026": ("Health Net HMO", "Gold"),
    "healthnet_platinum90_hmo_2026": ("Health Net HMO", "Platinum"),
    "lacare_bronze60_hmo_2026": ("L.A. Care HMO", "Bronze"),
    "lacare_silver70_hmo_2026": ("L.A. Care HMO", "Silver"),
    "lacare_gold80_hmo_2026": ("L.A. Care HMO", "Gold"),
    "lacare_platinum90_hmo_2026": ("L.A. Care HMO", "Platinum"),
}
LA_REGIONS = {"Los Angeles County – Northeast (Region 15)": 15, "Los Angeles County – Southwest (Region 16)": 16}
# UC SHIP at UCLA: the calendar row label -> enrollee group. A year is the Fall, Winter and Spring quarters
# (coverage 09/21/26 - 09/19/27; Spring runs through the summer).
UCSHIP_PLAN = "anthem_ppo_ucship_ucla_2026_27"
UCSHIP_GROUPS = {"Undergraduates": "undergraduate", "Graduates": "graduate"}
UCSHIP_TERMS = ("Fall Quarter", "Winter Quarter", "Spring Quarter")

PREMIUM_COLUMNS = ["plan_id", "rating_region", "age", "enrollee", "period", "amount_usd", "annual_usd", "basis",
                   "source_url", "source_page", "quote", "notes"]
AGE_COLUMNS = ["age", "factor", "source_url", "source_page", "quote"]
REGION_COLUMNS = ["zip", "county", "rating_region", "source_url", "source_page"]


def pages(pdf: Path) -> list[str]:
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    return text.split("\f")


def money(cell: str) -> float | None:
    cell = cell.strip()
    return None if cell in ("-", "") else float(cell.replace("$", "").replace(",", ""))


def booklet_rows(pdf: Path) -> list[dict]:
    """Every Los Angeles row of the booklet: region, age, plan line, metal -> monthly premium."""
    out = []
    for n, page in enumerate(pages(pdf), 1):
        region = next((r for title, r in LA_REGIONS.items() if title in page), None)
        age = re.search(r"(\d+)-year-old Single Individual", page)
        if region is None or age is None:
            continue
        for line in page.splitlines():
            m = re.match(r"\s*(\S.*?)\s{2,}((?:\$[\d,]+|-)(?:\s+(?:\$[\d,]+|-)){5})\s*$", line)
            if not m:
                continue
            label, cells = m.group(1), m.group(2).split()
            for metal, cell in zip(METALS, cells):
                out.append({"region": region, "age": int(age.group(1)), "label": label.rstrip("*").strip(),
                            "limited": label.endswith("*"), "metal": metal, "monthly": money(cell), "page": n,
                            "quote": line.strip()})
    return out


def covered_california(pdf: Path) -> tuple[list[dict], list[str]]:
    rows, missing = booklet_rows(pdf), []
    out = []
    for plan_id, (label, metal) in PLAN_ROWS.items():
        found = [r for r in rows if r["label"] == label and r["metal"] == metal and r["monthly"] is not None]
        if not found:
            missing.append(f"{plan_id}: the booklet has no {label} {metal} premium in Los Angeles")
        for r in found:
            notes = f"booklet line {label!r}, {metal} column"
            if r["limited"]:
                notes += "; marked * (not available in every ZIP code in the county)"
            out.append({"plan_id": plan_id, "rating_region": r["region"], "age": r["age"], "enrollee": None,
                        "period": "month", "amount_usd": r["monthly"], "annual_usd": round(r["monthly"] * 12, 2),
                        "basis": "covered_california_2026_preliminary", "source_url": CCA_BOOKLET,
                        "source_page": r["page"], "quote": " ".join(r["quote"].split()), "notes": notes})
    return out, missing


def ucship(pdf: Path) -> list[dict]:
    lines = pages(pdf)[0].splitlines()
    out = []
    for label, group in UCSHIP_GROUPS.items():
        terms = []
        for term in UCSHIP_TERMS:
            start = next(i for i, line in enumerate(lines) if line.strip().startswith(term))
            row = next(line for line in lines[start + 1:] if re.match(rf"\s*{label}\*?\s", line))
            terms.append((term, money(re.search(r"\$[\d,.]+", row).group()), row.strip()))
        amounts = {a for _, a, _ in terms}
        if len(amounts) != 1:
            raise ValueError(f"UC SHIP {label}: the quarters differ {terms}")
        out.append({"plan_id": UCSHIP_PLAN, "rating_region": None, "age": None, "enrollee": group,
                    "period": "quarter", "amount_usd": terms[0][1], "annual_usd": round(sum(a for _, a, _ in terms), 2),
                    "basis": "ucla_ucship_2026_27", "source_url": UCLA_CALENDAR, "source_page": 1,
                    "quote": " | ".join(f"{term}: {' '.join(row.split())}" for term, _, row in terms),
                    "notes": "UCLA quarter-system students; a year is Fall + Winter + Spring (coverage 09/21/26 - "
                             "09/19/27). Law, medicine and summer programs have other premiums and are not included."})
    return out


def age_curve(pdf: Path) -> list[dict]:
    for n, page in enumerate(pages(pdf), 1):
        if "Federal default standard age curve" not in page:
            continue
        out = []
        for line in page.splitlines():
            for age, factor in re.findall(r"(0-14|64 and Older|\d{1,2})\s+(\d\.\d{3})", line):
                ages = range(0, 15) if age == "0-14" else [64] if age.startswith("64") else [int(age)]
                out += [{"age": a, "factor": float(factor), "source_url": CMS_AGE_CURVE, "source_page": n,
                         "quote": f"{age} {factor}"} for a in ages]
        if not out:  # the guidance text names the appendix before the table
            continue
        out.sort(key=lambda r: r["age"])
        if [r["age"] for r in out] != list(range(65)):
            raise ValueError("age curve: expected one factor for every age 0-64")
        return out
    raise ValueError("age curve: Appendix I not found")


def rating_regions(pdf: Path) -> list[dict]:
    out = {}
    for n, page in enumerate(pages(pdf), 1):
        for region, zip_code in re.findall(r"^\s*(1[56])\s+Los Angeles\s+037\s+(\d{5})\s", page, re.M):
            out.setdefault(zip_code, {"zip": zip_code, "county": "Los Angeles", "rating_region": int(region),
                                      "source_url": CCA_ZIPS, "source_page": n})
    return sorted(out.values(), key=lambda r: r["zip"])


def write(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows({c: "" if r.get(c) is None else r[c] for c in columns} for r in rows)


def main(pdfs: Path, out: Path) -> int:
    for name, (url, sha) in SOURCES.items():
        path = pdfs / name
        if not path.exists():
            print(f"missing {path}: download {url}")
            return 1
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            print(f"{path} changed since it was read (SHA-256); check the new version before using it")
            return 1
    cca, missing = covered_california(pdfs / "CCA_26_QHP_Plan_Rates_by_County.pdf")
    premiums = cca + ucship(pdfs / "ucla_ucship_2026_27_calendar.pdf")
    ages = age_curve(pdfs / "cms_age_curve_guidance_2016-12-16.pdf")
    zips = rating_regions(pdfs / "cca_rating_region_zip.pdf")
    write(out / "premiums.csv", PREMIUM_COLUMNS, premiums)
    write(out / "age_curve.csv", AGE_COLUMNS, ages)
    write(out / "rating_regions.csv", REGION_COLUMNS, zips)
    print(f"{len(premiums)} premium rows, {len(ages)} ages, {len(zips)} Los Angeles ZIP codes -> {out}")
    for m in missing:
        print("NO PREMIUM", m)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pdfs", type=Path, default=ROOT / "data" / "policy_pdfs" / "premiums")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "plans")
    args = ap.parse_args()
    sys.exit(main(args.pdfs, args.out))
