"""Read a medical bill photo into structured lines. The model only copies what is printed; Python checks the shape.

The prompt is the one the bill reader was trained and evaluated on (scripts/bills, ~/Downloads/bills-eval-100.jsonl),
so the fine-tuned Muse Glimmer sees exactly what it learned. Models are tried in the order of
llm_client.bill_readers(): the BILL_* model when set, then the main model.

read_bill() (a photo file) and read_bill_image() (image bytes, e.g. a rendered PDF page) return {"status": "read", "bill", "model"} or {"status": "unreadable", "message"}.
"""

import base64
import json
import re
from pathlib import Path

PROMPT = ("Read this medical bill. Return only JSON with these keys: provider_name, provider_npi, statement_date, "
          "lines, total_billed. Each item of lines has service_date, code, description, units, billed. Write dates as "
          "YYYY-MM-DD and amounts as numbers. Copy names, codes and descriptions exactly as printed. Use null for "
          "anything the bill does not show.")
READABLE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_LINES = 60
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_reply(text: str) -> dict | None:
    """The bill JSON from a raw reply: after Glimmer's last `<|message|>` marker, else the first JSON object."""
    if not text:
        return None
    marker = text.rfind("<|message|>")
    if marker >= 0:
        text = text[marker + len("<|message|>"):]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start = text.find("{")
    while start >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(text, start)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def _amount(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2) if number >= 0 else None


def _text(value):
    return " ".join(str(value).split()) if value not in (None, "") else None


def normalize(raw: dict) -> dict | None:
    """The bill with every field in its expected type (unreadable values become null), or None without lines."""
    lines = raw.get("lines")
    if not isinstance(lines, list):
        return None
    out_lines = []
    for item in lines[:MAX_LINES]:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("code"))
        units = _amount(item.get("units"))
        out_lines.append({
            "service_date": item["service_date"] if isinstance(item.get("service_date"), str)
            and DATE.match(item["service_date"]) else None,
            "code": code.upper() if code else None,
            "description": _text(item.get("description")),
            "units": units if units and units == int(units) else None,
            "billed": _amount(item.get("billed")),
        })
    if not out_lines:
        return None
    npi = re.sub(r"\D", "", str(raw.get("provider_npi") or ""))
    statement_date = raw.get("statement_date")
    return {
        "provider_name": _text(raw.get("provider_name")),
        "provider_npi": npi if len(npi) == 10 else None,
        "statement_date": statement_date if isinstance(statement_date, str) and DATE.match(statement_date) else None,
        "lines": out_lines,
        "total_billed": _amount(raw.get("total_billed")),
    }


def read_bill(path: Path, content_type: str, readers=None) -> dict:
    return read_bill_image(Path(path).read_bytes() if content_type in READABLE_TYPES else b"", content_type, readers)


def read_bill_image(data: bytes, content_type: str, readers=None) -> dict:
    if content_type not in READABLE_TYPES:
        return {"status": "unreadable", "message": "Only JPEG, PNG and WebP photos can be read. Ask the user for a "
                                                   "screenshot or a JPEG of the bill."}
    if readers is None:
        from llm_client import bill_readers

        readers = bill_readers()
    image = f"data:{content_type};base64," + base64.b64encode(data).decode("ascii")
    messages = [{"role": "user", "content": [{"type": "text", "text": PROMPT},
                                             {"type": "image_url", "image_url": {"url": image}}]}]
    problems = []
    for client, model in readers:
        try:
            response = client.chat.completions.create(model=model, messages=messages, temperature=0, max_tokens=4096)
            raw = parse_reply(response.choices[0].message.content or "")
        except Exception as error:  # a stopped or cold fine-tuned server: try the next model
            problems.append(f"{model}: {type(error).__name__}")
            continue
        bill = normalize(raw) if raw else None
        if bill is not None:
            return {"status": "read", "bill": bill, "model": model}
        problems.append(f"{model}: no bill lines in the reply")
    return {"status": "unreadable", "message": "The bill could not be read (" + "; ".join(problems) + "). Ask the "
                                               "user for a clearer photo, or for the provider, codes and amounts."}
