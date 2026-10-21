"""Extract a plan's benefit tables from its SBC (or EOC schedule) with the LLM, into the extraction CSVs.

    cd workflow && python ../scripts/policy/extract_sbc.py PDF PLAN_ID [--pages 2-6] [--summary-pages 1]
        [--source-url URL] [--eoc] [--out DIR]

Python finds the pages (SBC: page 1 "Important Questions" + the "What You Will Pay" table pages; an EOC needs
--pages), renders each page and sends the image plus its text layer to the model, one page per call. The model
returns JSON rows under the extraction rules; Python adds plan_id / source_url / source_page, drops rows repeated
across pages, and writes <out>/plans.csv + benefits.csv (default data/plans/extracted/<plan_id>/) in the same
format as the Perplexity extraction, for scripts/policy/build_seed.py.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import RateLimitError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workflow"))
from llm_client import fast_extra_body, llm_model, make_client  # noqa: E402

PLAN_COLUMNS = ["plan_id", "insurer", "plan_name", "plan_year", "plan_type", "network_name", "deductible_in_usd",
                "deductible_out_usd", "oop_max_in_usd", "oop_max_out_usd", "coinsurance_in_pct", "source_url",
                "verified"]
BENEFIT_COLUMNS = ["plan_id", "section", "service", "service_as_printed", "component", "network", "network_tier",
                   "copay_usd", "coinsurance_pct", "deductible_applies", "prior_auth", "covered", "limits_conditions",
                   "quote", "source_url", "source_page", "verified"]
ROW_FIELDS = BENEFIT_COLUMNS[1:14]  # what the model fills per row
SERVICES = ("primary_care_visit, specialist_visit, urgent_care, emergency_room, ambulance, lab_tests, xray, "
            "advanced_imaging, generic_drugs, preferred_brand_drugs, mental_health_outpatient, preventive_care")

RULES = """
OUTPUT RULES
- Return JSON only. Numbers as JSON numbers (25, not "$25"); unknown values as null; never guess or compute.
- One row = ONE price. If a cell lists several prices, split them into separate rows:
  * by place/tier: "No charge/visit (Ashe Ctr); $25 copayment/visit (UC Family)" -> one row with
    network_tier "Ashe Center", copay_usd 0, and one with network_tier "UC Family", copay_usd 25;
  * by sub-service: "X-ray 25% & Lab 20% coinsurance" -> one row service "xray", one row service "lab_tests";
  * by component: "Office visit: no charge. Facility: 10%. Provider: 10%" -> three rows with
    component "office_visit", "facility", "provider";
  * by setting: "20% + $125 per admission; 20% per admission for Ambulatory Surgical Center" -> two rows,
    the setting (e.g. "ASC") written in limits_conditions.
  Put copay_usd AND coinsurance_pct in one row ONLY when both apply to the same bill ("$250 copayment + 20%").
- Every pricing column/tier is its own row. network is "in" for every in-network tier, "out" for out-of-network.
  network_tier is the short tier or place name (e.g. "Ashe Center", "UC Family", "Network"), without the word
  "Provider", spelled the same on every row; null for out-of-network and for plans with a single in-network level.
  Never hide a tier's price in limits_conditions.
- Deductible (SBC convention), in this order: cell says "deductible does not apply" -> false; the service is in
  page 1's answer to "Are there services covered before you meet your deductible?" (given as context) -> false;
  cell says the deductible applies -> true; silent and that tier has a deductible -> true; silent and that tier
  has no deductible -> false.
- section = the "Common Medical Event" text the row sits under (an EOC: the benefit heading).
- service: after splitting, the most specific of: """ + SERVICES + """; otherwise "other".
  An x-ray row is "xray"; a blood-work row is "lab_tests". Drug tiers: generic / Tier 1 -> "generic_drugs";
  preferred brand / Tier 2 -> "preferred_brand_drugs"; non-preferred brand, Tier 3, specialty, Tier 4 -> "other".
- service_as_printed: the benefit name exactly as printed. component: "office_visit" | "facility" | "provider" | null.
- copay_usd: dollars per visit/prescription/admission, 0 for "no charge". coinsurance_pct: what the PATIENT pays,
  0-100 ("plan pays 80%" -> 20).
- deductible_applies / prior_auth / covered: true | false | null. "Not covered" -> covered false.
- limits_conditions: caps ("up to a maximum of $250"), visit limits, penalties, balance billing, setting notes.
- quote: the exact cell text the row comes from, verbatim (the whole cell).
- Items under "Services Your Plan Generally Does NOT Cover": one row each, section "Excluded services",
  service "other", network "in", covered false.
- Skip headers, footers, grievance/appeals text, language lines and the Coverage Examples.
"""

PAGE_PROMPT = RULES + """
Extract every benefit row on this page. Return {"rows": [ {""" + ", ".join(f'"{f}"' for f in ROW_FIELDS) + """} ]}.
Return {"rows": []} if the page has no benefit table.
"""

SUMMARY_PROMPT = RULES + """
This is the page with the plan's overall deductible and out-of-pocket limit ("Important Questions" in an SBC).
Return {"plan": {"insurer", "plan_name", "plan_year", "plan_type" (HMO|PPO|EPO|POS), "network_name",
"deductible_in_usd", "deductible_out_usd", "oop_max_in_usd", "oop_max_out_usd", "coinsurance_in_pct"},
"rows": [...]}.
plan: individual (single person) amounts; when in-network amounts differ by tier use the CHEAPEST tier
(a tier with no deductible -> 0); coinsurance_in_pct only if one in-network coinsurance applies to most services.
rows: one row per tier for the overall deductible and one per tier for the out-of-pocket limit, with
service "plan_level", section "Important Questions", service_as_printed "Overall deductible" or
"Out-of-pocket limit", the individual dollar amount in copay_usd, family amounts in limits_conditions.
If this page also has benefit rows, include them in rows too.
"""

BEFORE_DEDUCTIBLE_PROMPT = """
Below is the page of a health plan document that answers "Are there services covered before you meet your
deductible?" (or states which services the deductible does not apply to). Return JSON only:
{"services": [...]} listing ONLY the services from this list that the text explicitly says are covered before the
deductible / not subject to it: """ + SERVICES + """.
Mapping: "physician / family / primary care office visits" -> primary_care_visit; "specialist visits" ->
specialist_visit; "urgent care" -> urgent_care; "emergency room" -> emergency_room; "preventive care" ->
preventive_care; "generic drugs" / "tier 1" -> generic_drugs; "mental health / behavioral health office visits" ->
mental_health_outpatient; "lab" -> lab_tests; "x-ray" -> xray. Vague phrases ("and other services") add nothing.
Return {"services": []} if the plan has no deductible or the text names no services.
"""
DEDUCTIBLE_STATED = re.compile(r"deductible\s+(applies|then)|after\s+(the\s+)?deductible|subject\s+to\s+(the\s+)?"
                               r"deductible", re.I)

EOC_NOTE = """
NOTE: this page is from an Evidence of Coverage "Schedule of Cost Share and Benefits", not an SBC. section = the
benefit heading the row sits under. For an EPO/HMO, out-of-network cells that say "Not Covered" -> covered false.
"""


def page_texts(pdf):
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    return out.split("\f")


def find_pages(texts):
    """(summary page, benefit pages), 1-based, for an SBC: the federal template wording is fixed."""
    summary = next((i + 1 for i, t in enumerate(texts) if re.search(r"Important Questions", t)), None)
    benefit = [i + 1 for i, t in enumerate(texts)
               if re.search(r"Common\s+Medical\s+Event|Services\s+You\s+May\s+Need|What\s+You\s+Will\s+Pay", t)
               or re.search(r"Generally\s+Does\s+NOT\s+Cover", t)]
    return summary, benefit


def render(pdf, page, tmp):
    prefix = Path(tmp) / f"p{page}"
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "200", "-png", "-singlefile", str(pdf),
                    str(prefix)], check=True)
    return base64.b64encode(prefix.with_suffix(".png").read_bytes()).decode("ascii")


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return json.loads(fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1])


def ask(client, model, prompt, pages, context=""):
    """pages: [(page number, base64 PNG, text layer)], sent together in one call."""
    content = [{"type": "text", "text": "CONTEXT ONLY, do not extract rows from it: the plan's deductible page(s), "
                                        f"for the deductible rule:\n{context}"}] if context else []
    for page, image, text in pages:
        content += [{"type": "text", "text": f"PDF page {page}. Its text layer (layout preserved):\n\n{text}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}]
    page = ",".join(str(p) for p, _, _ in pages)
    last = None
    # A dense page needs ~5k tokens. At temperature 0 the model sometimes falls into a repetition loop and runs to
    # the cap; the retry samples at a higher temperature, since the same greedy input would loop the same way.
    for temperature in (0, 0.4, 0.7):
        for wait in (20, 40, 80, 160, None):  # the shared endpoint caps concurrent requests (HTTP 429)
            try:
                # Thinking off: with it on, a dense page spends many minutes reasoning before the first token.
                resp = client.chat.completions.create(
                    model=model, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                    temperature=temperature, max_tokens=8000,
                    extra_body=fast_extra_body())
                break
            except RateLimitError:
                if wait is None:
                    raise
                print(f"  page {page}: rate limited, retrying in {wait}s", flush=True)
                time.sleep(wait)
        try:
            return parse_json(resp.choices[0].message.content)
        except (ValueError, json.JSONDecodeError) as e:
            last = e
            print(f"  page {page}: unusable output ({resp.choices[0].finish_reason}), retrying", flush=True)
    raise ValueError(f"page {page}: model did not return JSON ({last})")


def csv_value(v):
    if v is None:
        return "NOT FOUND"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return v


def tier_name(tier):
    """One spelling per tier across pages: "UC Family Provider" -> "UC Family", "Ashe Ctr" -> "Ashe Center"."""
    tier = re.sub(r"\s+", " ", tier or "").strip()
    tier = re.sub(r"\s*\(.*?\)\s*$", "", tier)  # "(You will pay the least)"
    tier = re.sub(r"\s+providers?$", "", tier, flags=re.I)
    tier = re.sub(r"\bCtr\b\.?", "Center", tier, flags=re.I)
    if re.fullmatch(r"tier\s*\d+", tier, re.I):  # a drug tier, never a network tier
        return ""
    return "" if tier.lower() in ("", "in-network", "in network", "out-of-network", "out of network") else tier


def assemble(plan_id, source_url, summary, pages):
    """Plan row + benefit rows from model output; drop exact repeats (tables continued across pages)."""
    plan = {k: csv_value((summary.get("plan") or {}).get(k)) for k in PLAN_COLUMNS}
    plan.update(plan_id=plan_id, source_url=source_url or "NOT FOUND", verified="")
    rows, seen, dropped = [], set(), 0
    for page, rows_out in pages:
        for r in rows_out:
            row = {f: csv_value(r.get(f)) for f in ROW_FIELDS}
            for text_field in ("component", "network_tier", "limits_conditions", "section"):
                row[text_field] = "" if row[text_field] == "NOT FOUND" else row[text_field]
            row["network_tier"] = tier_name(row["network_tier"]) if row["network"] == "in" else ""
            key = tuple(str(row[f]) for f in ("section", "service", "service_as_printed", "component", "network",
                                              "network_tier"))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            rows.append({**row, "plan_id": plan_id, "source_url": source_url or "NOT FOUND", "source_page": page,
                         "verified": ""})
    return plan, rows, dropped


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, columns)
        w.writeheader()
        w.writerows(rows)


def before_deductible(client, model, summary_text):
    """The standard services page 1 says are covered before the deductible (a choice from fixed options)."""
    resp = client.chat.completions.create(
        model=model, temperature=0, max_tokens=500, extra_body=fast_extra_body(),
        messages=[{"role": "system", "content": BEFORE_DEDUCTIBLE_PROMPT}, {"role": "user", "content": summary_text}])
    allowed = {s.strip() for s in SERVICES.split(",")}
    return sorted(set(parse_json(resp.choices[0].message.content).get("services") or []) & allowed)


def apply_before_deductible(rows, services):
    """SBC convention: a service the plan covers before the deductible doesn't use it, unless its own cell says it
    does. Returns how many rows changed."""
    changed = 0
    for r in rows:
        if (r["network"] == "in" and r["service"] in services and r["deductible_applies"] != "no"
                and not DEDUCTIBLE_STATED.search(str(r["quote"]))):
            r["deductible_applies"] = "no"
            changed += 1
    return changed


def renormalize(out):
    """Re-apply the Python-side cleanup (tier spelling) to an extraction already written, without the model."""
    rows = list(csv.DictReader(open(out / "benefits.csv", newline="", encoding="utf-8")))
    for r in rows:
        r["network_tier"] = tier_name(r["network_tier"]) if r["network"] == "in" else ""
    write_csv(out / "benefits.csv", BENEFIT_COLUMNS, rows)


def parse_pages(spec):
    pages = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        pages += range(int(a), int(b or a) + 1)
    return pages


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("plan_id")
    ap.add_argument("--pages", help="benefit pages, e.g. 2-6 or 15-29 (default: found automatically in an SBC)")
    ap.add_argument("--summary-pages", help="pages with deductible / out-of-pocket limit, e.g. 1 or 16-17 "
                                            "(default: found automatically in an SBC)")
    ap.add_argument("--source-url", default="")
    ap.add_argument("--eoc", action="store_true", help="the pages are an EOC Schedule of Cost Share and Benefits")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--renormalize", action="store_true", help="only re-apply the tier cleanup to an existing output")
    ap.add_argument("--deductible-only", action="store_true",
                    help="only re-run the covered-before-deductible step on an existing output")
    args = ap.parse_args()
    out = args.out or ROOT / "data" / "plans" / "extracted" / args.plan_id
    if args.renormalize:
        renormalize(out)
        return

    client, model = make_client(timeout=600.0, max_retries=2), llm_model()

    texts = page_texts(args.pdf)
    found_summary, found_pages = find_pages(texts)
    summary_pages = parse_pages(args.summary_pages) if args.summary_pages else [found_summary] if found_summary else []
    pages = parse_pages(args.pages) if args.pages else found_pages
    if not summary_pages or not pages:
        sys.exit(f"could not find the pages (summary {summary_pages}, benefits {pages}); "
                 "pass --summary-pages/--pages")
    pages = [p for p in pages if p not in summary_pages]
    note = EOC_NOTE if args.eoc else ""
    context = "\n\n".join(texts[p - 1] for p in summary_pages)
    print(f"{args.plan_id}: summary pages {summary_pages}, benefit pages {pages}", flush=True)
    if args.deductible_only:
        rows = list(csv.DictReader(open(out / "benefits.csv", newline="", encoding="utf-8")))
        services = before_deductible(client, model, context)
        changed = apply_before_deductible(rows, services)
        write_csv(out / "benefits.csv", BENEFIT_COLUMNS, rows)
        print(f"{args.plan_id}: covered before the deductible {services}; {changed} rows set to no")
        return

    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(args.workers) as pool:
        def run(page_list, prompt, ctx):
            got = ask(client, model, note + prompt, [(p, render(args.pdf, p, tmp), texts[p - 1]) for p in page_list],
                      ctx)
            print(f"  page {','.join(map(str, page_list))}: {len(got.get('rows') or [])} rows", flush=True)
            return got
        summary_future = pool.submit(run, summary_pages, SUMMARY_PROMPT, "")
        page_futures = [(p, pool.submit(run, [p], PAGE_PROMPT, context)) for p in pages]
        summary = summary_future.result()
        page_rows = [(summary_pages[0], summary.get("rows") or [])] + [(p, f.result().get("rows") or [])
                                                                        for p, f in page_futures]

    plan, rows, dropped = assemble(args.plan_id, args.source_url, summary, page_rows)
    services = before_deductible(client, model, context)
    changed = apply_before_deductible(rows, services)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "plans.csv", PLAN_COLUMNS, [plan])
    write_csv(out / "benefits.csv", BENEFIT_COLUMNS, rows)
    print(f"{args.plan_id}: {len(rows)} rows ({dropped} repeats dropped; covered before the deductible {services}, "
          f"{changed} rows set to no) -> {out}")

if __name__ == "__main__":
    main()
