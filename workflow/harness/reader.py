"""Read an uploaded plan PDF. Runs when the user attaches a file, before the model sees the turn.

1. Render page 1 and send it to the AMD vision model for the plan's identity only: plan name,
   insurer, networks, plan year and document type. No benefit numbers are taken from this read.
2. Python matches the identity and the PDF's own text against the plan library
   (policies/catalog.json). A matched plan is priced from its human-checked rules file.

read_plan_pdf() is an async generator: ("progress", step) any number of times, then ("result", r).
"""

import asyncio
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "policies" / "catalog.json"

IDENTITY_PROMPT = """
This is the first page of a health insurance document. Read only what identifies the plan.
Return one JSON object and nothing else:

{
  "document_type": "benefit booklet" | "summary of benefits and coverage" | "certificate" | "flyer" | "other",
  "plan_name": string | null,
  "insurer": string | null,
  "plan_year": string | null,
  "network_names": [string],
  "for_whom": string | null
}

Copy names as printed. Use null or [] when the page doesn't show it. Don't read benefit amounts.
"""


def load_catalog(path=CATALOG_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))["plans"]


def match_plan(text, catalog):
    """The catalog plan whose match phrases all appear in text, or None. Case-insensitive."""
    lower = text.lower()
    for plan in catalog:
        rule = plan["match"]
        if all(p.lower() in lower for p in rule.get("all", [])) and \
                any(p.lower() in lower for p in rule.get("any", [""])):
            return plan
    return None


def page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def page_text(pdf, first=1, last=2):
    return subprocess.run(["pdftotext", "-f", str(first), "-l", str(last), "-layout", str(pdf), "-"],
                          capture_output=True, text=True).stdout


def page_image(pdf, page=1, dpi=150):
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-png", str(pdf), str(prefix)],
                       check=True, capture_output=True)
        return next(Path(tmp).glob("page*.png")).read_bytes()


def thumbnail(pdf, page=1):
    """A small JPEG data URL of the page, for the progress panel."""
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "thumb"
        subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "60", "-jpeg", str(pdf), str(prefix)],
                       check=True, capture_output=True)
        data = next(Path(tmp).glob("thumb*.jpg")).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def parse_json(text):
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


def read_identity(client, model, png):
    response = client.responses.create(
        model=model,
        instructions=IDENTITY_PROMPT,
        input=[{"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")},
        ]}],
        max_output_tokens=3000,
        store=False,
    )
    return parse_json(response.output_text)


async def read_plan_pdf(client, model, pdf, name, catalog=None):
    catalog = catalog if catalog is not None else load_catalog()
    title = name or Path(pdf).name

    def step(step_id, status, text, screenshot=None):
        return "progress", {"step_id": step_id, "status": status, "text": text, "url": None,
                            "title": title, "screenshot": screenshot}

    pages = await asyncio.to_thread(page_count, pdf)
    thumb = await asyncio.to_thread(thumbnail, pdf)
    yield step("open", "done", f"Opened {title} ({pages} pages)", thumb)

    yield step("identify", "running", "Reading page 1 with the AMD model…")
    try:
        png = await asyncio.to_thread(page_image, pdf)
        identity = await asyncio.to_thread(read_identity, client, model, png)
    except Exception as error:  # the read is a convenience; matching below still runs on the PDF text
        identity = {}
        yield step("identify", "failed", f"Couldn't read page 1 ({type(error).__name__})", thumb)
    else:
        shown = " · ".join(str(v) for v in (identity.get("plan_name"), identity.get("insurer"),
                                             identity.get("plan_year")) if v)
        yield step("identify", "done", f"Found: {shown or 'no plan name on page 1'}", thumb)

    yield step("match", "running", "Looking for this plan in the verified plan library…")
    text = await asyncio.to_thread(page_text, pdf)
    plan = match_plan(text + "\n" + json.dumps(identity, ensure_ascii=False), catalog)
    rules = ROOT / plan["rules"] if plan else None
    if plan and rules.exists():
        yield step("match", "done", f"Matched {plan['name']} in the verified plan library")
    elif plan:
        yield step("match", "failed", f"Recognized {plan['name']}, but its rules aren't loaded yet")
    else:
        yield step("match", "failed", "This plan isn't in the verified plan library yet")
    yield "result", {"pages": pages, "identity": identity, "plan": plan,
                     "rules_path": str(rules) if plan and rules.exists() else None}
