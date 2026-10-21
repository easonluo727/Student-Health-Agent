"""Read an uploaded plan PDF. Runs when the user attaches a file, before the model sees the turn.

1. Render page 1 and send it to the vision model for the plan's identity only: plan name,
   insurer, networks, plan year and document type. No benefit numbers are taken from this read.
2. Python matches the identity and the PDF's own text against the plan database
   (data/plans/policy.sqlite). A matched plan is priced from the database, never from the upload.

read_plan_pdf() is an async generator: ("progress", step) any number of times, then ("result", r).
"""

import asyncio
import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

from llm_client import fast_extra_body


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


def match_plan(text, catalog):
    """The plan whose match phrases all appear in text, or None. Case-insensitive."""
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
    """The model's JSON object, tolerating thinking, a code fence or words around it."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    text = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    return json.loads(text)


def read_identity(client, model, png):
    # Thinking off: with it on, the reasoning could use up the output budget and return no JSON.
    response = client.chat.completions.create(
        model=model, temperature=0, max_tokens=800,
        messages=[{"role": "system", "content": IDENTITY_PROMPT}, {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
        ]}],
        extra_body=fast_extra_body(),
    )
    return parse_json(response.choices[0].message.content)


def db_catalog():
    """The plan database (data/plans/policy.sqlite) as a match catalog: a plan matches when its name as printed
    (the part before " - ") appears, with its insurer, and "Los Angeles" when the name names the campus."""
    from plan_db import PlanDB

    catalog = []
    for p in PlanDB().plans():
        name = p["plan_name"].split(" - ")[0].strip()
        required = [name] + (["Los Angeles"] if "Los Angeles" in p["plan_name"] else [])
        catalog.append({"plan_id": p["plan_id"], "name": p["plan_name"], "insurer": p["insurer"],
                        "plan_year": p["plan_year"],
                        "match": {"all": required, "any": [p["insurer"].split()[0]]}})
    # Longer names first, so "Silver 70 Ambetter HMO" is tried before a shorter name it contains.
    return sorted(catalog, key=lambda plan: -len(plan["match"]["all"][0]))


async def read_plan_pdf(client, model, pdf, name, catalog):
    title = name or Path(pdf).name

    def step(step_id, status, text, screenshot=None):
        return "progress", {"step_id": step_id, "status": status, "text": text, "url": None,
                            "title": title, "screenshot": screenshot}

    pages = await asyncio.to_thread(page_count, pdf)
    thumb = await asyncio.to_thread(thumbnail, pdf)
    yield step("open", "done", f"Opened {title} ({pages} pages)", thumb)

    yield step("identify", "running", "Reading page 1…")
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

    yield step("match", "running", "Looking for this plan in the plan database…")
    text = await asyncio.to_thread(page_text, pdf)
    plan = match_plan(text + "\n" + json.dumps(identity, ensure_ascii=False), catalog)
    if plan:
        yield step("match", "done", f"Matched {plan['name']} in the plan database")
    else:
        yield step("match", "failed", "This plan isn't in the plan database yet")
    yield "result", {"pages": pages, "identity": identity, "plan": plan}
